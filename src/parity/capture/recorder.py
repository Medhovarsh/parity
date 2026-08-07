"""Live capture by wrapping a provider.

``Recorder`` satisfies the provider port itself, so it drops in wherever your
application already calls a model:

    provider = Recorder(OpenAICompatibleProvider(), store=store)

Every completion passes straight through and is also written to the baseline,
redacted first. Failures are never recorded — a baseline must contain what the
model actually produced, not what it failed to.

Buffered by default so capture does not add a synchronous disk write to every
request on a hot path. Call :meth:`flush`, or use it as a context manager.
"""

from __future__ import annotations

from types import TracebackType

from parity.domain.models import Case, InteractionInput, InteractionOutput, ModelRef
from parity.ports.provider import LLMProvider
from parity.ports.store import BaselineStore
from parity.security.redaction import RedactionReport, Redactor

DEFAULT_BUFFER_SIZE = 32


class Recorder:
    """Provider decorator that captures every successful completion."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        store: BaselineStore,
        redactor: Redactor | None = None,
        tags: tuple[str, ...] = (),
        buffer_size: int = DEFAULT_BUFFER_SIZE,
        enabled: bool = True,
    ) -> None:
        self._provider = provider
        self._store = store
        self._redactor = redactor
        self._tags = tags
        self._buffer_size = max(1, buffer_size)
        self._enabled = enabled
        self._buffer: list[Case] = []
        self.redaction = RedactionReport()
        self.captured = 0

    @property
    def name(self) -> str:
        return self._provider.name

    def complete(self, model: str, request: InteractionInput) -> InteractionOutput:
        output = self._provider.complete(model, request)
        if self._enabled:
            self._record(model, request, output)
        return output

    def _record(self, model: str, request: InteractionInput, output: InteractionOutput) -> None:
        case = Case.create(
            input=request,
            output=output,
            reference=ModelRef(provider=self._provider.name, model=model),
            tags=self._tags,
        )
        if self._redactor is not None:
            case, report = self._redactor.case(case)
            self.redaction.merge(report)
        self._buffer.append(case)
        if len(self._buffer) >= self._buffer_size:
            self.flush()

    def flush(self) -> int:
        """Write buffered cases to the store. Returns how many were new."""
        if not self._buffer:
            return 0
        pending, self._buffer = self._buffer, []
        written = self._store.extend(pending)
        self.captured += written
        return written

    def close(self) -> None:
        try:
            self.flush()
        finally:
            self._provider.close()

    def __enter__(self) -> Recorder:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # Flush even on the error path: cases captured before the failure are
        # still valid evidence and throwing them away helps nobody.
        self.close()
