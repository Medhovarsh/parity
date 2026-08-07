"""Ports: the interfaces the core depends on.

The core imports *only* from here. Concrete implementations live in
``parity.adapters`` and are selected at the edge, by the CLI or by an embedding
application. That inversion is what lets the entire test suite run offline
against a fake provider, and what lets a user add a provider without forking.

These are ``typing.Protocol`` definitions, checked structurally. An adapter does
not need to inherit from anything — it needs to have the right shape.
"""

from parity.ports.clock import Clock
from parity.ports.judge import SemanticJudge
from parity.ports.provider import LLMProvider
from parity.ports.store import BaselineStore, RunStore

__all__ = ["BaselineStore", "Clock", "LLMProvider", "RunStore", "SemanticJudge"]
