"""Stable, machine-readable error codes for every fail-closed surface.

Inspired by REA's taxonomy (stable codes + canonical meaning, e.g.
`capability_unavailable`): rejection answers carry a code suffix so a judging
model can react (`[code=E_PATH_PROTECTED]`) instead of parsing prose, while
the human message stays intact. The registry below is exhaustive: raise
sites must pick from it, and a code not in CODES with a raise site is a bug
(see tests/test_error_codes.py). Unknown codes downstream must fall back to
the prose, not guess.
"""

CODES: dict[str, str] = {
    # guardrails (paths)
    "E_PATH_WRITABLE": "The target path is outside writable_roots.",
    "E_PATH_PROTECTED": "The target path matches a deny_globs protected pattern.",
    # guardrails (shell)
    "E_SHELL_EMPTY": "The shell command was empty.",
    "E_SHELL_UNPARSEABLE": "The shell command could not be tokenized safely.",
    "E_SHELL_PATH_BINARY": "The shell binary was a path, not a bare allowlisted name.",
    "E_SHELL_NOT_ALLOWLISTED": "The shell binary is not in guardrails.shell_allowlist.",
    "E_SHELL_NO_POLICY": "The shell binary is allowlisted but has no sandbox mode.",
    "E_SHELL_BIN_MISSING": "The shell binary did not resolve on PATH.",
    "E_SHELL_BIN_UNTRUSTED": "The shell binary resolves outside trusted_bin_dirs.",
    "E_SHELL_FLAG_DENY": "The shell command used a flag that enables arbitrary execution.",
    "E_SHELL_ARG_REJECTED": "A path argument failed the deny/writable checks.",
    # global controls
    "E_KILL_SWITCH": "The operator kill switch file is present in data_dir.",
    # capabilities (REA / inspect_application)
    "E_CAP_DISABLED": "The capability is present but not enabled in config.",
    "E_CAP_BIN_BARE": "A capability binary was configured as a path instead of a bare name.",
    "E_CAP_TOKEN_INVALID": "A capability command prefix token contains paths, whitespace, or metacharacters.",
    "E_CAP_BIN_MISSING": "A capability binary did not resolve on PATH.",
    "E_TARGET_MISSING": "The requested target path does not exist.",
}


def suffix(code: str | None) -> str:
    """Message suffix that carries the code into human/LLM-readable text."""
    return f" [code={code}]" if code else ""


def explain(code: str) -> str:
    try:
        return CODES[code]
    except KeyError:
        return "unknown code — treat the prose reason as authoritative"
