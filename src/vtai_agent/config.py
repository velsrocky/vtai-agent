from pathlib import Path
import sys
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

import os


def _find_config_toml() -> Path:
    """Resolution order: $VT_CONFIG, the repo-adjacent config (editable
    installs / development), then the user-level config written by
    `vtai init`. A global `uv tool` install has no repo to be adjacent
    to, so the user-level file is what it finds."""
    env = os.environ.get("VT_CONFIG")
    if env:
        return Path(env).expanduser()
    repo = Path(__file__).resolve().parents[2] / "config" / "settings.toml"
    if repo.exists():
        return repo
    return Path.home() / ".vtaiagent" / "settings.toml"


CONFIG_TOML = _find_config_toml()


class _Priced(BaseModel):
    """USD per 1,000 tokens. None = fall back to pydantic-ai's native cost
    table, then 0.0 (free)."""
    cost_per_1k_input: float | None = Field(default=None, ge=0)
    cost_per_1k_output: float | None = Field(default=None, ge=0)


class ProviderAnthropic(_Priced):
    model: str = "claude-sonnet-4-5"


class ProviderOpenAI(_Priced):
    model: str = "gpt-5"


class ProviderLocal(_Priced):
    """Any OpenAI-compatible endpoint: llama.cpp server, Ollama, vLLM, LM Studio."""
    base_url: str = "http://localhost:8080/v1"
    model: str = "qwen2.5-vl-7b-instruct"
    api_key: str = "not-needed"


# run_shell only executes binaries resolving under one of these dirs.
def _default_trusted_bin_dirs() -> list[str]:
    if sys.platform == "win32":
        return [r"C:\Windows\System32", r"C:\Windows"]
    if sys.platform == "darwin":
        return ["/usr/bin", "/bin", "/usr/local/bin", "/opt/homebrew/bin"]
    return ["/usr/bin", "/bin", "/usr/local/bin"]


class Guardrails(BaseModel):
    dry_run_default: bool = True
    max_steps: int = 40
    max_retries: int = 3
    budget_usd_per_run: float = 1.0
    shell_allowlist: list[str] = Field(default_factory=list)
    deny_globs: list[str] = Field(default_factory=list)
    # run_shell only executes binaries resolving under one of these dirs.
    trusted_bin_dirs: list[str] = Field(default_factory=_default_trusted_bin_dirs)
    # Echo-loop breaker: after this many denials of the *same* action+detail
    # within denial_window_seconds, subsequent denial messages carry a
    # CIRCUIT BREAKER note telling the model to stop retrying.
    max_repeat_denials: int = Field(default=3, ge=1)
    denial_window_seconds: float = Field(default=300, ge=1)
    # Extend the built-in fail-closed shell policy: map an allowlisted binary
    # to how its path arguments should be treated.
    shell_extra_modes: dict[str, Literal["none", "read", "write"]] = Field(
        default_factory=dict,
    )


class Media(BaseModel):
    vaapi_device: str = "/dev/dri/renderD128"
    video_codec: str = "h264_vaapi"
    container: str = "mkv"
    output_dir_name: str = "transcoded"


class Paths(BaseModel):
    model_config = ConfigDict(validate_default=True)

    writable_roots: list[Path] = Field(default_factory=list)
    data_dir: Path = Path("~/.vtaiagent")

    @field_validator("writable_roots", "data_dir", mode="before")
    @classmethod
    def _expand(cls, v):
        if isinstance(v, list):
            return [Path(x).expanduser() for x in v]
        return Path(v).expanduser()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VT_",
        env_file=".env",
        env_nested_delimiter="__",
        extra="ignore",
    )

    active_provider: Literal["anthropic", "openai", "local"] = "local"
    anthropic: ProviderAnthropic = Field(default_factory=ProviderAnthropic)
    openai: ProviderOpenAI = Field(default_factory=ProviderOpenAI)
    local: ProviderLocal = Field(default_factory=ProviderLocal)
    guardrails: Guardrails = Field(default_factory=Guardrails)
    paths: Paths = Field(default_factory=Paths)
    media: Media = Field(default_factory=Media)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        # Priority: init args > env > .env > config/settings.toml
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls, CONFIG_TOML),
            file_secret_settings,
        )

    @property
    def data_dir(self) -> Path:
        d = self.paths.data_dir
        d.mkdir(parents=True, exist_ok=True)
        return d


_settings: Settings | None = None


def validate_settings(s: Settings) -> None:
    """Fail fast on malformed, silently-dangerous configuration. Called once
    at startup so a bad settings.toml surfaces immediately, not mid-run."""
    import fnmatch
    import re

    errs: list[str] = []
    g = s.guardrails
    if g.max_steps < 1:
        errs.append("guardrails.max_steps must be >= 1")
    if g.budget_usd_per_run < 0:
        errs.append("guardrails.budget_usd_per_run must be >= 0")
    if not g.shell_allowlist:
        errs.append("guardrails.shell_allowlist is empty — run_shell will allow nothing")
    for d in g.shell_extra_modes.values():
        if d not in ("none", "read", "write"):
            errs.append(f"invalid shell_extra_modes value: {d}")
    unknown = set(g.shell_extra_modes) - set(g.shell_allowlist)
    if unknown:
        errs.append(f"shell_extra_modes references binaries not in shell_allowlist: {sorted(unknown)}")
    for pat in g.deny_globs:
        if not isinstance(pat, str) or not pat.strip():
            errs.append(f"deny_globs contains an empty/invalid pattern: {pat!r}")
            continue
        try:
            fnmatch.translate(pat)
        except re.error:
            errs.append(f"deny_globs pattern does not compile: {pat!r}")
    for d in g.trusted_bin_dirs:
        if not Path(d).is_dir():
            errs.append(f"trusted_bin_dirs entry is not a directory: {d}")
    for r in s.paths.writable_roots:
        if not Path(r).exists():
            errs.append(f"writable_roots entry does not exist: {r}")
    if errs:
        raise ValueError("invalid configuration:\n  - " + "\n  - ".join(errs))


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
        validate_settings(_settings)
    return _settings
