"""Guardrails: the safety layer that makes unattended operation acceptable.

Every privileged action (write to disk, run a shell command) passes through
here. Anything not explicitly allowed is denied, and every decision — allowed
or denied — is written to the audit log.
"""

import fnmatch
import os
import shlex
from dataclasses import dataclass
from pathlib import Path

from .config import Guardrails as GuardrailsConfig
from .config import get_settings
from . import db


class GuardrailViolation(PermissionError):
    """Raised when an action is outside the allowlist."""


@dataclass
class ShellDecision:
    argv: list[str]
    command: str
    allowed: bool
    reason: str = ""


class Guardrails:
    def __init__(self, cfg: GuardrailsConfig | None = None):
        self.cfg = cfg or get_settings().guardrails
        self._deny = [self._expand_glob(g) for g in self.cfg.deny_globs]

    # ---------- paths ----------
    def check_writable(self, path: str | Path) -> Path:
        p = Path(path).expanduser()
        self._check_deny(p)
        roots = [Path(r).expanduser() for r in get_settings().paths.writable_roots]
        if not any(self._is_within(p, r) for r in roots):
            self._deny_action("write", str(p), f"outside writable_roots: {roots}")
            raise GuardrailViolation(f"Not writable (outside allowlist): {p}")
        return p

    def check_readable(self, path: str | Path) -> Path:
        p = Path(path).expanduser()
        self._check_deny(p)
        return p

    def _check_deny(self, p: Path) -> None:
        for pat in self._deny:
            if fnmatch.fnmatch(str(p), pat):
                self._deny_action("access", str(p), f"matches deny glob {pat}")
                raise GuardrailViolation(f"Protected path: {p}")

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except ValueError:
            return path == root

    @staticmethod
    def _expand_glob(g: str) -> str:
        return str(Path(g).expanduser())

    # ---------- shell ----------
    def check_shell(self, command: str) -> ShellDecision:
        try:
            argv = shlex.split(command)
        except ValueError as e:
            d = ShellDecision([], command, False, f"unparseable: {e}")
            self._deny_action("shell", command, d.reason)
            return d
        if not argv:
            d = ShellDecision([], command, False, "empty command")
            self._deny_action("shell", command, d.reason)
            return d
        binary = Path(argv[0]).name
        if binary not in self.cfg.shell_allowlist:
            d = ShellDecision(argv, command, False, f"binary '{binary}' not in allowlist")
            self._deny_action("shell", command, d.reason)
            return d
        return ShellDecision(argv, command, True)

    # ---------- dry run / budget / kill ----------
    @property
    def dry_run_default(self) -> bool:
        return self.cfg.dry_run_default

    def kill_switch_active(self) -> bool:
        """A kill file drops to abort immediately; absence mode polls this."""
        return (get_settings().data_dir / "KILL").exists()

    # ---------- audit ----------
    def _deny_action(self, action: str, detail: str, reason: str) -> None:
        db.audit(None, action, f"{detail} :: {reason}", allowed=False)
