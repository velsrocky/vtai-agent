from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

CONFIG_TOML = Path(__file__).resolve().parents[2] / "config" / "settings.toml"


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


class Guardrails(BaseModel):
    dry_run_default: bool = True
    max_steps: int = 40
    max_retries: int = 3
    budget_usd_per_run: float = 1.0
    shell_allowlist: list[str] = Field(default_factory=list)
    deny_globs: list[str] = Field(default_factory=list)
    # run_shell only executes binaries resolving under one of these dirs.
    trusted_bin_dirs: list[str] = ["/usr/bin", "/bin", "/usr/local/bin"]
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


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
