"""LLM provider abstraction.

Supports both cloud (Anthropic, OpenAI) and local (any OpenAI-compatible
endpoint: llama.cpp server, Ollama, vLLM) models. The local path costs nothing
per call and keeps secrets on-box — it's the default so the agent is useful
before any API key is configured.

API keys are read from the standard env vars the coding-agent CLIs already use
(ANTHROPIC_API_KEY / OPENAI_API_KEY), optionally overridden with
VT_ANTHROPIC__API_KEY / VT_OPENAI__API_KEY.
"""

import os

from pydantic_ai.models import Model

from .config import Settings, get_settings


def _env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


class ProviderNotConfigured(RuntimeError):
    pass


def resolve_api_key(provider: str, settings: Settings) -> str:
    if provider == "anthropic":
        return _env("VT_ANTHROPIC__API_KEY", "ANTHROPIC_API_KEY") or ""
    if provider == "openai":
        return _env("VT_OPENAI__API_KEY", "OPENAI_API_KEY") or ""
    return settings.local.api_key


def build_model(settings: Settings | None = None) -> Model:
    """Construct the pydantic-ai Model for the active provider."""
    settings = settings or get_settings()
    active = settings.active_provider

    if active == "anthropic":
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        key = resolve_api_key("anthropic", settings)
        if not key:
            raise ProviderNotConfigured(
                "active_provider=anthropic but no key. "
                "Set ANTHROPIC_API_KEY (or VT_ANTHROPIC__API_KEY) in .env."
            )
        return AnthropicModel(
            settings.anthropic.model, provider=AnthropicProvider(api_key=key)
        )

    if active == "openai":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        key = resolve_api_key("openai", settings)
        if not key:
            raise ProviderNotConfigured(
                "active_provider=openai but no key. "
                "Set OPENAI_API_KEY (or VT_OPENAI__API_KEY) in .env."
            )
        return OpenAIChatModel(
            settings.openai.model, provider=OpenAIProvider(api_key=key)
        )

    # local OpenAI-compatible endpoint
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    return OpenAIChatModel(
        settings.local.model,
        provider=OpenAIProvider(
            base_url=settings.local.base_url, api_key=settings.local.api_key
        ),
    )


def estimate_step_cost(settings: Settings, usage: object) -> float:
    """USD for one agent step. Explicit `cost_per_1k_*` config wins; else
    pydantic-ai's native cost table when the model has one; else 0 (free/local)."""
    provider = getattr(settings, settings.active_provider)
    p_in = getattr(provider, "cost_per_1k_input", None)
    p_out = getattr(provider, "cost_per_1k_output", None)
    if p_in is not None or p_out is not None:
        return ((getattr(usage, "input_tokens", 0) or 0) / 1000 * (p_in or 0.0)
                + (getattr(usage, "output_tokens", 0) or 0) / 1000 * (p_out or 0.0))
    cost = getattr(usage, "cost", None)
    return float(cost) if cost is not None else 0.0


def describe_active(settings: Settings | None = None) -> dict:
    """Human/CLI-readable summary of the configured brain."""
    settings = settings or get_settings()
    a = settings.active_provider
    if a == "anthropic":
        return {"provider": a, "model": settings.anthropic.model,
                "key_configured": bool(resolve_api_key("anthropic", settings))}
    if a == "openai":
        return {"provider": a, "model": settings.openai.model,
                "key_configured": bool(resolve_api_key("openai", settings))}
    return {"provider": "local", "model": settings.local.model,
            "base_url": settings.local.base_url, "key_configured": True}
