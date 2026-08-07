"""Process exit codes.

Distinct codes matter in CI: a pipeline should be able to tell "the model
regressed" (act on it) from "the config is wrong" (fix the pipeline) without
parsing stderr.
"""

from __future__ import annotations

from enum import IntEnum


class ExitCode(IntEnum):
    OK = 0
    """Everything ran and the gate passed."""

    GATE_FAILED = 1
    """The run completed and found regressions. The only 'expected' failure."""

    CONFIG_ERROR = 2
    """Bad configuration, unknown provider, unusable arguments."""

    STORE_ERROR = 3
    """A baseline or run report could not be read or written."""

    SECURITY_LIMIT = 4
    """Input exceeded a configured resource limit and was refused."""

    INTERRUPTED = 130
    """Ctrl-C. Matches the shell convention of 128 + SIGINT."""

    INTERNAL_ERROR = 70
    """An unhandled defect in Parity itself. Worth a bug report."""
