"""Provider port.

Deliberately synchronous. Concurrency is the *runner's* concern and is handled
with a thread pool, which keeps adapters trivial to write and lets a contributor
add a provider without understanding an async stack. Providers are I/O-bound, so
threads are the right tool here regardless.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from parity.domain.models import InteractionInput, InteractionOutput


@runtime_checkable
class LLMProvider(Protocol):
    """Something that can turn an interaction input into an output."""

    @property
    def name(self) -> str:
        """Short provider identifier, e.g. ``openai``. Used in ``ModelRef``."""
        ...

    def complete(self, model: str, request: InteractionInput) -> InteractionOutput:
        """Run one completion.

        Must raise :class:`parity.errors.ProviderError` on failure, with
        ``retryable`` set correctly — the runner uses that flag rather than
        inspecting provider-specific error text.
        """
        ...

    def close(self) -> None:
        """Release any held resources. Must be idempotent."""
        ...
