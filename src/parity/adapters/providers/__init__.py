"""Provider adapters.

``fake`` is the only one that runs with no network and no credentials, and it is
what the entire test suite uses. ``ollama`` is the recommended zero-cost path for
real models on a developer machine.
"""

from parity.adapters.providers.anthropic import AnthropicProvider
from parity.adapters.providers.fake import FakeProvider, Mutation
from parity.adapters.providers.ollama import OllamaProvider
from parity.adapters.providers.openai_compat import OpenAICompatibleProvider
from parity.adapters.providers.registry import PROVIDER_KINDS, build_provider

__all__ = [
    "PROVIDER_KINDS",
    "AnthropicProvider",
    "FakeProvider",
    "Mutation",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "build_provider",
]
