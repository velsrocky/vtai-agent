"""Guardrails: the safety layer that makes unattended operation acceptable.

Every privileged action (write to disk, run a shell command) passes through
here. Anything not explicitly allowed is denied, and every decision — allowed
or denied — is written to the audit log.

Shell policy is fail-closed: a command runs only when its binary is a bare
name, allowlisted, resolves inside a trusted system bin dir, has an explicit
sandbox mode (none | read | write), carries no dangerous flags, and every
path argument passes the same deny/writable checks the file tools use.
"""

import fnmatch
import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import Guardrails as GuardrailsConfig
from .config import Settings, get_settings
from . import db

TRUSTED_BIN_DIRS: tuple[str, ...] = ("/usr/bin", "/bin", "/usr/local/bin")

# Commands that take no interesting arguments.
SHELL_MODE_NONE = frozenset({"df", "free", "nproc", "uptime", "rocm-smi", "vulkaninfo"})
# Commands whose path arguments are read-only: deny-globs still apply.
SHELL_MODE_READ = frozenset({"ls", "cat", "head", "tail", "wc", "du", "grep", "file", "stat"})
# Commands that mutate the filesystem: every non-flag argument must be inside
# writable_roots.
SHELL_MODE_WRITE = frozenset({"mkdir", "mv", "cp", "rsync", "tar", "zip", "unzip"})

# Flags that turn an allowlisted binary into an arbitrary code/file executor.
SHELL_DENY_FLAGS: dict[str, tuple[str, ...]] = {
    "rsync": ("--rsync-path", "--rsh", "-e"),
    "tar": ("--checkpoint-action", "--to-command", "--use-compress-program", "-I"),
    "zip": ("--pipe", "-TT"),
}


class GuardrailViolation(PermissionError):
    """Raised when an action is outside the allowlist."""


@dataclass
class ShellDecision:
    argv: list[str]
    command: str
    allowed: bool
    reason: str = ""


class Guardrails:
    def __init__(self, cfg: GuardrailsConfig | None = None,
                 settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.cfg = cfg if cfg is not None else self.settings.guardrails
        self._deny = [self._expand_glob(g) for g in self.cfg.deny_globs]
        self._trusted = tuple(self.cfg.trusted_bin_dirs) or TRUSTED_BIN_DIRS

    # ---------- paths ----------
    def check_writable(self, path: str | Path) -> Path:
        p = Path(path).expanduser().resolve()
        self._check_deny(p)
        roots = [Path(r).expanduser().resolve() for r in self.settings.paths.writable_roots]
        if not any(self._is_within(p, r) for r in roots):
            self._deny_action("write", str(p), f"outside writable_roots: {roots}")
            raise GuardrailViolation(f"Not writable (outside allowlist): {p}")
        return p

    def check_readable(self, path: str | Path) -> Path:
        p = Path(path).expanduser().resolve()
        self._check_deny(p)
        return p

    def _check_deny(self, p: Path) -> None:
        s = str(p)
        for pat in self._deny:
            if fnmatch.fnmatch(s, pat) or self._within_literal_prefix(s, pat):
                self._deny_action("access", s, f"matches deny glob {pat}")
                raise GuardrailViolation(f"Protected path: {p}")

    def is_denied(self, path: str | Path) -> bool:
        """Non-raising check used to filter scan results out of reports."""
        try:
            s = str(Path(path).expanduser().resolve())
        except OSError:
            return True
        return any(
            fnmatch.fnmatch(s, pat) or self._within_literal_prefix(s, pat)
            for pat in self._deny
        )

    @staticmethod
    def _within_literal_prefix(path_str: str, pattern: str) -> bool:
        """Deny `~/.ssh/**` must also protect `~/.ssh` itself, not just children."""
        prefix = pattern.split("*", 1)[0].rstrip("/")
        if not prefix:
            return False
        return path_str == prefix or path_str.startswith(prefix + os.sep)

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
    def check_shell(self, command: str,
                    base_dir: str | Path | None = None) -> ShellDecision:
        """Validate a command. Relative path arguments are resolved against
        base_dir (where the command will actually run), not the agent's cwd."""
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return self._reject_shell([], command, f"unparseable: {e}")
        if not argv:
            return self._reject_shell([], command, "empty command")

        binary = argv[0]
        if os.sep in binary:
            return self._reject_shell(argv, command,
                                      f"binary must be a bare name, not a path: {binary}")
        if binary not in self.cfg.shell_allowlist:
            return self._reject_shell(argv, command, f"binary '{binary}' not in allowlist")

        mode = self._mode_for(binary)
        if mode is None:
            return self._reject_shell(argv, command,
                                      f"'{binary}' is allowlisted but has no sandbox policy")

        found = shutil.which(binary)
        if found is None:
            return self._reject_shell(argv, command, f"'{binary}' not found on PATH")
        real = str(Path(found).resolve())
        if not real.startswith(self._trusted):
            return self._reject_shell(argv, command,
                                      f"'{binary}' resolves outside trusted dirs: {real}")

        for arg in argv[1:]:
            for flag in SHELL_DENY_FLAGS.get(binary, ()):
                if arg == flag or arg.startswith(f"{flag}=") or (
                        flag.startswith("-") and not flag.startswith("--")
                        and arg.startswith(flag)):
                    return self._reject_shell(argv, command,
                                              f"flag '{arg}' not permitted for '{binary}'")

        path_args = [a for a in argv[1:] if not a.startswith("-")]
        try:
            if mode == "write":
                for a in path_args:
                    self.check_writable(self._anchored(a, base_dir))
            elif mode == "read":
                for a in path_args:
                    if self._looks_like_path(a, base_dir):
                        self.check_readable(self._anchored(a, base_dir))
        except GuardrailViolation as e:
            return self._reject_shell(argv, command, f"argument rejected: {e}")

        return ShellDecision([real, *argv[1:]], command, True)

    @staticmethod
    def _anchored(arg: str, base_dir: str | Path | None) -> Path:
        p = Path(arg).expanduser()
        if base_dir is not None and not p.is_absolute():
            return Path(base_dir).expanduser() / p
        return p

    def _reject_shell(self, argv: list[str], command: str, reason: str) -> ShellDecision:
        d = ShellDecision(argv, command, False, reason)
        self._deny_action("shell", command, reason)
        return d

    def _mode_for(self, binary: str) -> str | None:
        extra = self.cfg.shell_extra_modes.get(binary)
        if extra is not None:
            return extra
        if binary in SHELL_MODE_NONE:
            return "none"
        if binary in SHELL_MODE_READ:
            return "read"
        if binary in SHELL_MODE_WRITE:
            return "write"
        return None

    @staticmethod
    def _looks_like_path(arg: str, base_dir: str | Path | None = None) -> bool:
        if "/" in arg or arg.startswith("~"):
            return True
        return Guardrails._anchored(arg, base_dir).exists()

    # ---------- dry run / budget / kill ----------
    @property
    def dry_run_default(self) -> bool:
        return self.cfg.dry_run_default

    def kill_switch_active(self) -> bool:
        """A kill file drops to abort immediately; absence mode polls this."""
        return (self.settings.paths.data_dir / "KILL").exists()

    def ensure_not_killed(self) -> None:
        if self.kill_switch_active():
            self._deny_action("abort", "kill switch", "operator kill switch active")
            raise GuardrailViolation("Kill switch active — refusing to act")

    # ---------- audit ----------
    def _deny_action(self, action: str, detail: str, reason: str) -> None:
        db.audit(None, action, f"{detail} :: {reason}", allowed=False)
