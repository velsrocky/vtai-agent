import shutil
import types
from pathlib import Path

import pytest

from vtai_agent import config, db
from vtai_agent.config import Guardrails as GuardrailsConfig, Paths, Settings

TEST_BINS = ("ls", "cat", "cp", "mkdir", "rsync", "tar", "grep", "uptime")


def _trusted_bin_dirs() -> list[str]:
    """Where the test binaries actually live on this machine (dev boxes may
    shadow coreutils, e.g. cargo coreutils)."""
    dirs = {"/usr/bin", "/bin", "/usr/local/bin"}
    for b in TEST_BINS:
        found = shutil.which(b)
        if found:
            dirs.add(str(Path(found).resolve()).rsplit("/", 1)[0])
    return sorted(dirs)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Isolated settings: writable dir, protected dir, tmp data dir.

    Patches the global settings so db.audit (which uses get_settings) also
    writes into tmp_path, and resets the cached engine each test.
    """
    writable = tmp_path / "writable"
    protected = tmp_path / "protected"
    writable.mkdir()
    protected.mkdir()
    (tmp_path / "data").mkdir()
    (protected / "secret.txt").write_text("secret")

    cfg = GuardrailsConfig(
        dry_run_default=True,
        shell_allowlist=list(TEST_BINS),
        deny_globs=[str(protected / "**"), "~/.ssh/**"],
        trusted_bin_dirs=_trusted_bin_dirs(),
        max_steps=10,
        max_retries=2,
    )
    paths = Paths(writable_roots=[writable], data_dir=tmp_path / "data")
    settings = Settings(guardrails=cfg, paths=paths)

    monkeypatch.setattr(config, "_settings", settings)
    monkeypatch.setattr(db, "_engine", None)

    return types.SimpleNamespace(
        settings=settings, cfg=cfg, paths=paths,
        writable=writable, protected=protected, tmp=tmp_path,
    )
