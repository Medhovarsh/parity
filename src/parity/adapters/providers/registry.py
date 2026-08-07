"""Provider construction from declarative configuration."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from parity.adapters.providers.anthropic import AnthropicProvider
from parity.adapters.providers.fake import FakeProvider, Mutation
from parity.adapters.providers.http_base import DEFAULT_TIMEOUT_SECONDS
from parity.adapters.providers.ollama import DEFAULT_BASE_URL as OLLAMA_BASE_URL
from parity.adapters.providers.ollama import OllamaProvider
from parity.adapters.providers.openai_compat import OpenAICompatibleProvider
from parity.errors import ConfigError
from parity.ports.provider import LLMProvider

ProviderKind = Literal["fake", "openai", "anthropic", "ollama"]

PROVIDER_KINDS: tuple[str, ...] = ("fake", "openai", "anthropic", "ollama")

#: Sensible defaults per kind, so a user configuring a well-known provider only
#: has to name it. ``openai`` doubles as the generic OpenAI-compatible adapter —
#: point ``base_url`` at any server exposing ``/chat/completions``.
_DEFAULTS: dict[str, tuple[str, str | None]] = {
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "anthropic": ("https://api.anthropic.com/v1", "ANTHROPIC_API_KEY"),
    "ollama": (OLLAMA_BASE_URL, None),
    "fake": ("", None),
}


class ProviderConfig(BaseModel):
    """How to construct one provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ProviderKind = "openai"
    name: str | None = None
    """Identifier used in ``provider:model`` references. Defaults to ``kind``."""

    base_url: str | None = None
    api_key_env: str | None = None
    """Environment variable holding the credential. The key itself is never
    stored in config, so a config file is safe to commit."""

    require_key: bool = True
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0)
    headers: dict[str, str] = Field(default_factory=dict)
    mutation: Mutation = Mutation.NONE
    """Fake provider only. Ignored by every other kind."""

    def resolved_name(self) -> str:
        return self.name or self.kind


def build_provider(config: ProviderConfig) -> LLMProvider:
    """Instantiate the provider described by ``config``."""
    kind = config.kind
    if kind not in _DEFAULTS:
        raise ConfigError(
            f"unknown provider kind {kind!r}; expected one of {', '.join(PROVIDER_KINDS)}"
        )

    default_url, default_key_env = _DEFAULTS[kind]
    base_url = config.base_url or default_url
    api_key_env = config.api_key_env if config.api_key_env is not None else default_key_env
    name = config.resolved_name()

    if kind == "fake":
        return FakeProvider(name=name, mutation=config.mutation)

    if kind == "ollama":
        return OllamaProvider(
            name=name,
            base_url=base_url,
            timeout=config.timeout_seconds,
            extra_headers=config.headers or None,
        )

    if kind == "anthropic":
        return AnthropicProvider(
            name=name,
            base_url=base_url,
            api_key_env=api_key_env,
            require_key=config.require_key,
            timeout=config.timeout_seconds,
            extra_headers=config.headers or None,
        )

    return OpenAICompatibleProvider(
        name=name,
        base_url=base_url,
        api_key_env=api_key_env,
        require_key=config.require_key,
        timeout=config.timeout_seconds,
        extra_headers=config.headers or None,
    )
