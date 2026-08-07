"""Parity — a behavioural regression gate for non-deterministic LLM systems.

Parity captures what your model *actually did* on real inputs, replays those
inputs against a candidate model or prompt, and classifies every difference as
equivalent, acceptable, or broken. The point is to make a model migration or a
prompt edit reviewable, instead of a leap of faith.

The public surface is deliberately small. Everything else is an implementation
detail and may change between minor versions before 1.0.
"""

from parity.domain.models import (
    Case,
    CaseOutcome,
    CheckResult,
    CheckStatus,
    InteractionInput,
    InteractionOutput,
    Message,
    ModelRef,
    RunReport,
    Severity,
    ToolCall,
    Verdict,
)
from parity.errors import (
    ConfigError,
    ParityError,
    ProviderError,
    SecurityLimitExceeded,
    StoreError,
)

__version__ = "0.2.0"

__all__ = [
    "Case",
    "CaseOutcome",
    "CheckResult",
    "CheckStatus",
    "ConfigError",
    "InteractionInput",
    "InteractionOutput",
    "Message",
    "ModelRef",
    "ParityError",
    "ProviderError",
    "RunReport",
    "SecurityLimitExceeded",
    "Severity",
    "StoreError",
    "ToolCall",
    "Verdict",
    "__version__",
]
