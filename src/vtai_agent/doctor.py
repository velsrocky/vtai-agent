"""Preflight checks — is this machine set up to run the agent?

`vtai doctor` answers the questions a new user has, in one place, without
having to know which command surfaces which problem: is uv new enough, which
config file is in effect and does it validate, are the writable roots there,
what is protected, which optional tools are installed, is the model
reachable, is the MCP server registered, and is a delegate waiting for
review.

Every check is defensive: doctor must report problems, never raise them.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

OK = "ok"
WARN = "warn"
FAIL = "fail"

_GLYPH = {OK: "✓", WARN: "!", FAIL: "✗"}

# Keep in sync with [tool.uv] required-version in pyproject.toml
# (tests/test_doctor.py asserts they match).
MIN_UV = (0, 13, 1)

# Optional tools: which vtai feature needs them.
OPTIONAL_TOOLS = {
    "rsync": "backup_sync",
    "ffmpeg": "transcode_media, voice helpers",
}

CLOUD_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


class Report:
    """Collects check results and prints them as they are found."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str, str]] = []
        self.failures = 0
        self.warnings = 0

    def add(self, label: str, status: str, detail: str = "") -> None:
        self.lines.append((label, status, detail))
        if status == FAIL:
            self.failures += 1
        elif status == WARN:
            self.warnings += 1

    def render(self) -> str:
        width = max((len(label) for label, _, _ in self.lines), default=0)
        out = []
        for label, status, detail in self.lines:
            line = f"  {_GLYPH[status]}  {label.ljust(width)}"
            if detail:
                line += f"  {detail}"
            out.append(line)
        return "\n".join(out)


def _uv_version() -> tuple[str | None, str | None]:
    """(version, raw). None when uv is not on PATH."""
    uv = shutil.which("uv")
    if not uv:
        return None, None
    try:
        raw = subprocess.run([uv, "--version"], capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None, None
    # "uv 0.13.1 (Homebrew 2026-10-14 aarch64-apple-darwin)"
    parts = raw.split()
    version = parts[1] if len(parts) > 1 and parts[0] == "uv" else None
    return version, raw


def _version_tuple(v: str) -> tuple[int, ...]:
    out = []
    for part in v.split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def _probe_url(url: str, timeout: float = 1.5) -> bool:
    """Is something listening at this URL? TCP only — no request is sent."""
    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
        host = parsed.hostname
        if not host:
            return False
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def _mcp_registrations() -> dict[str, str]:
    """Where the vtai-agent MCP server is registered, per client."""
    found: dict[str, str] = {}
    candidates = {
        "opencode (global)": Path.home() / ".config" / "opencode" / "opencode.json",
        "opencode (project)": Path.cwd() / "opencode.json",
    }
    for label, path in candidates.items():
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        servers = (data.get("mcp") or {}).get("servers") or {}
        if "vtai-agent" in servers:
            found[label] = str(path)
    claude = Path.home() / ".claude.json"
    try:
        if claude.is_file():
            data = json.loads(claude.read_text())
            if "vtai-agent" in (data.get("mcpServers") or {}):
                found["claude code (user)"] = str(claude)
    except (OSError, ValueError):
        pass
    return found


def _pending_approvals() -> list:
    try:
        from . import db

        return list(db.list_pending_approvals())
    except Exception:
        return []


def run_doctor() -> int:
    """Run every check, print the report, return 0 (healthy) or 1 (a FAIL)."""
    from . import __version__
    from .config import CONFIG_TOML, config_source, get_settings

    r = Report()
    r.add("agent version", OK, __version__)

    # --- uv -------------------------------------------------------------
    version, raw = _uv_version()
    if version is None:
        r.add("uv", WARN, "not on PATH — the installed environment still works, "
                          "but `uv sync`/`uv tool` will not")
    elif _version_tuple(version) < MIN_UV:
        r.add("uv", FAIL, f"{version} is older than the required "
                          f"{'.'.join(str(p) for p in MIN_UV)} (lockfile revision 5)")
    else:
        r.add("uv", OK, version)

    # --- config file ----------------------------------------------------
    source = config_source()
    if CONFIG_TOML.is_file():
        note = f"{CONFIG_TOML} ({source})"
        if source == "repo-adjacent":
            note += " — this is the checkout's config, not yours; set VT_CONFIG"
        r.add("config file", OK if source != "repo-adjacent" else WARN, note)
    else:
        r.add("config file", FAIL, f"missing: {CONFIG_TOML} — run `vtai init`")

    # --- config validity ------------------------------------------------
    settings = None
    try:
        settings = get_settings()
        r.add("config valid", OK, "no issues")
    except ValueError as e:
        # The message starts with a generic "invalid configuration:" header;
        # the lines below it are the actual causes.
        lines = [ln.strip(" -") for ln in str(e).splitlines()[1:] if ln.strip()]
        detail = "; ".join(lines[:2]) if lines else str(e).splitlines()[0]
        r.add("config valid", FAIL, f"{detail} — run `vtai init`")
    except Exception as e:  # pragma: no cover - defensive
        r.add("config valid", FAIL, str(e))

    if settings is not None:
        missing = [str(p) for p in settings.paths.writable_roots
                   if not Path(p).exists()]
        if missing:
            r.add("writable roots", FAIL,
                  f"{len(missing)} missing: {', '.join(missing[:3])}"
                  + (" …" if len(missing) > 3 else ""))
        else:
            r.add("writable roots", OK,
                  f"{len(settings.paths.writable_roots)} present")

        n_deny = len(settings.guardrails.deny_globs)
        if n_deny:
            r.add("protected paths", OK, f"{n_deny} deny globs")
        else:
            r.add("protected paths", WARN, "deny_globs is empty — nothing is protected")

        n_allow = len(settings.guardrails.shell_allowlist)
        if n_allow:
            r.add("shell allowlist", OK, f"{n_allow} binaries")
        else:
            r.add("shell allowlist", WARN, "empty — run_shell will allow nothing")

        # --- provider ---------------------------------------------------
        provider = settings.active_provider
        if provider == "local":
            base = settings.local.base_url
            if _probe_url(base):
                r.add("model endpoint", OK, f"{base}")
            else:
                r.add("model endpoint", FAIL,
                      f"{base} is unreachable — start the server or point "
                      f"VT_LOCAL__BASE_URL at one")
        else:
            key = CLOUD_KEYS.get(provider, "")
            if key and os.environ.get(key):
                r.add("model endpoint", OK, f"{provider} ({key} set)")
            elif key:
                r.add("model endpoint", FAIL, f"{key} is not set")
            else:
                r.add("model endpoint", WARN, f"unknown provider {provider!r}")

    # --- optional tools --------------------------------------------------
    missing_tools = [f"{name} (for {why})" for name, why in OPTIONAL_TOOLS.items()
                     if not shutil.which(name)]
    if missing_tools:
        r.add("optional tools", WARN, "not installed: " + ", ".join(missing_tools))
    else:
        r.add("optional tools", OK, ", ".join(OPTIONAL_TOOLS))

    # --- voice -----------------------------------------------------------
    helpers = [h for h in ("agent-listen", "agent-speak") if shutil.which(h)]
    base = os.environ.get("LEMONADE_BASE_URL", "http://localhost:13305")
    if _probe_url(base):
        voice = "server reachable"
    else:
        voice = f"no server at {base}"
    if helpers:
        r.add("voice", OK if "reachable" in voice else WARN,
              f"{len(helpers)}/2 helpers on PATH, {voice}")
    else:
        r.add("voice", WARN, f"helpers not installed (`vtai voice-sync`), {voice}")

    # --- MCP registration -------------------------------------------------
    registrations = _mcp_registrations()
    if registrations:
        r.add("mcp registration", OK, ", ".join(sorted(registrations)))
    else:
        r.add("mcp registration", WARN,
              "not registered — run scripts/register-mcp.sh")

    # --- pending approvals -------------------------------------------------
    pending = _pending_approvals()
    if pending:
        r.add("pending approvals", WARN,
              f"{len(pending)} waiting — `vtai approve`")
    else:
        r.add("pending approvals", OK, "none")

    print(f"VT-AIAgent doctor — v{__version__}")
    print(r.render())
    if r.failures:
        print(f"\n{r.failures} problem(s) need fixing; {r.warnings} warning(s).")
    else:
        print("\nAll good." + (f" ({r.warnings} warning(s))" if r.warnings else ""))
    return 1 if r.failures else 0


def main() -> int:  # pragma: no cover - thin wrapper
    return run_doctor()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
