"""File organization — the first real workflow (Tier A).

Content-aware: categorizes by extension, then confirms ambiguous items with the
`file` command's mime type. Moves stay inside writable_roots, collisions are
auto-renamed, and dry_run returns the full plan before anything moves.
"""

import asyncio
import shutil
from pathlib import Path

from pydantic import BaseModel, Field

from .. import db
from ..guardrails import GuardrailViolation, Guardrails
from .registry import Tool, ToolResult

CATEGORY_BY_EXT: dict[str, str] = {}
for _cat, _exts in {
    "Documents": ["pdf", "docx", "doc", "txt", "odt", "rtf", "epub", "pages", "tex"],
    "Spreadsheets": ["xlsx", "xls", "csv", "ods", "numbers"],
    "Presentations": ["pptx", "ppt", "odp", "key"],
    "Images": ["jpg", "jpeg", "png", "gif", "webp", "svg", "heic", "heif", "tiff",
               "tif", "bmp", "raw", "cr2", "nef", "arw", "psd", "ai", "indd"],
    "Videos": ["mp4", "mkv", "avi", "mov", "webm", "flv", "wmv", "m4v", "mpg",
               "mpeg", "3gp", "ts"],
    "Audio": ["mp3", "wav", "flac", "aac", "ogg", "m4a", "alac", "aiff", "opus", "wma"],
    "Archives": ["zip", "tar", "gz", "bz2", "xz", "zst", "7z", "rar", "iso", "dmg"],
    "Installers": ["deb", "rpm", "appimage", "flatpak", "snap", "exe", "msi", "apk"],
    "Code": ["py", "js", "mjs", "ts", "jsx", "tsx", "html", "htm", "css", "scss",
             "json", "yaml", "yml", "toml", "ini", "sh", "bash", "zsh", "go", "rs",
             "java", "kt", "cpp", "cc", "c", "h", "hpp", "cs", "rb", "php", "sql",
             "md", "rst", "tex", "gitignore", "lock", "diff", "patch"],
    "Data": ["db", "sqlite", "sqlite3", "parquet", "feather", "pkl", "pickle", "h5"],
    "Fonts": ["ttf", "otf", "woff", "woff2", "eot"],
    "Models": ["gguf", "ggml", "onnx", "pt", "pth", "safetensors", "bin", "ckpt"],
}.items():
    for _e in _exts:
        CATEGORY_BY_EXT[_e.lower()] = _cat

MIME_HINTS = {
    "image": "Images", "video": "Videos", "audio": "Audio",
    "text": "Documents", "pdf": "Documents", "archive": "Archives",
    "compressed": "Archives", "font": "Fonts",
}


class OrganizeFilesInput(BaseModel):
    directory: str = Field(default="~/Downloads", description="Directory to tidy up.")
    dry_run: bool | None = Field(
        default=None,
        description="Plan only, move nothing. Defaults to guardrails.dry_run_default.",
    )
    recursive: bool = Field(default=False, description="Descend into subdirectories.")
    confirm_mime: bool = Field(
        default=True,
        description="Use `file` mime type to resolve unknown extensions."
    )


class OrganizeFilesTool(Tool[OrganizeFilesInput]):
    name = "organize_files"
    description = (
        "Content-aware cleanup of a directory: sorts files into Documents/Images/"
        "Videos/Audio/Archives/Installers/Code/Data/Fonts/Models, auto-renames "
        "collisions. Destination stays inside the directory, within writable_roots."
    )
    InputModel = OrganizeFilesInput

    def __init__(self, guardrails: Guardrails | None = None):
        self.g = guardrails or Guardrails()

    async def run(self, inp: OrganizeFilesInput, *, run_id: int | None = None) -> ToolResult:
        dry_run = inp.dry_run if inp.dry_run is not None else self.g.dry_run_default
        src = self.g.check_readable(inp.directory)
        if not src.is_dir():
            raise NotADirectoryError(f"not a directory: {src}")
        # every move lands inside src, so the whole operation must be writable
        self.g.check_writable(src)

        plan = await self._plan(src, inp)
        if dry_run:
            db.audit(run_id, "organize_files", f"[dry-run] {len(plan)} items in {src}",
                     allowed=True)
            return ToolResult(
                ok=True,
                summary=f"DRY RUN: would organize {len(plan)} files in {src}",
                detail={"plan": [p.model_dump() for p in plan], "dry_run": True},
                dry_run=True,
            )

        moved, skipped = await self._execute(plan, run_id)
        by_cat: dict[str, int] = {}
        for p in plan:
            by_cat[p.category] = by_cat.get(p.category, 0) + 1
        return ToolResult(
            ok=True,
            summary=f"Organized {moved} files in {src} ({skipped} skipped)",
            detail={"moved": moved, "skipped": skipped, "by_category": by_cat,
                    "plan": [p.model_dump() for p in plan]},
        )

    async def _plan(self, src: Path, inp: OrganizeFilesInput) -> list["PlannedMove"]:
        candidates = (
            list(src.rglob("*")) if inp.recursive else list(src.iterdir())
        )
        files = [f for f in candidates if f.is_file()]
        mime_cache: dict[str, str] = {}
        if inp.confirm_mime and files:
            mime_cache = await self._mime_types(files)

        plan: list[PlannedMove] = []
        for f in files:
            ext = f.suffix.lstrip(".").lower()
            category = CATEGORY_BY_EXT.get(ext)
            if category is None and mime_cache:
                category = self._category_from_mime(mime_cache.get(str(f), ""))
            if category is None:
                category = "Other"
            dest_dir = src / category
            plan.append(PlannedMove(
                source=str(f), destination=str(dest_dir / f.name), category=category,
            ))
        return plan

    async def _mime_types(self, files: list[Path]) -> dict[str, str]:
        out: dict[str, str] = {}
        if shutil.which("file") is None:
            return out
        try:
            proc = await asyncio.create_subprocess_exec(
                "file", "--mime-type", "-b", *[str(f) for f in files[:200]],
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            for f, line in zip(files[:200], stdout.decode(errors="replace").splitlines()):
                out[str(f)] = line.strip()
        except (asyncio.TimeoutError, OSError):
            pass
        return out

    @staticmethod
    def _category_from_mime(mime: str) -> str | None:
        if not mime:
            return None
        for key, cat in MIME_HINTS.items():
            if key in mime:
                return cat
        return None

    async def _execute(self, plan: list["PlannedMove"], run_id: int | None) -> tuple[int, int]:
        self.g.ensure_not_killed()
        moved = skipped = 0
        for p in plan:
            src_path = Path(p.source)
            dest_path = Path(p.destination)
            if not src_path.exists():
                skipped += 1
                continue
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path = self._resolve_collision(dest_path)
            p.destination = str(dest_path)
            try:
                self.g.check_writable(dest_path)
                shutil.move(str(src_path), str(dest_path))
                db.audit(run_id, "move", f"{src_path} -> {dest_path}", allowed=True)
                moved += 1
            except (OSError, GuardrailViolation) as e:
                db.audit(run_id, "move", f"{src_path} -> {dest_path}", allowed=False)
                p.error = str(e)
                skipped += 1
        return moved, skipped

    @staticmethod
    def _resolve_collision(dest: Path) -> Path:
        if not dest.exists():
            return dest
        stem, suffix, parent = dest.stem, dest.suffix, dest.parent
        for i in range(1, 10000):
            cand = parent / f"{stem}_{i}{suffix}"
            if not cand.exists():
                return cand
        raise FileExistsError(f"no free name for {dest}")


class PlannedMove(BaseModel):
    source: str
    destination: str
    category: str
    error: str | None = None
