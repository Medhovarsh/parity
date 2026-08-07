"""Deterministic checks.

Every check in this package runs locally, costs nothing, and needs no network.
That is the point: the majority of behaviour regressions after a model swap are
structural — output stopped parsing, a field vanished, a tool stopped being
called, generation got truncated — and none of those require a language model to
detect.

A semantic judge is only consulted for what survives this layer.
"""

from parity.checks.base import Check, CheckContext
from parity.checks.registry import ALL_CHECKS, build_pipeline, check_names

__all__ = ["ALL_CHECKS", "Check", "CheckContext", "build_pipeline", "check_names"]
