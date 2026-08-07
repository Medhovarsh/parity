"""Exception hierarchy.

Every error Parity raises deliberately derives from :class:`ParityError`, so a
caller embedding the library can catch one type. The CLI maps each subclass to a
distinct exit code — see :mod:`parity.cli.exit_codes`.
"""

from __future__ import annotations


class ParityError(Exception):
    """Base class for every error raised by Parity."""


class ConfigError(ParityError):
    """Configuration is missing, malformed, or internally inconsistent."""


class StoreError(ParityError):
    """A baseline or run store could not be read or written."""


class ProviderError(ParityError):
    """A model provider failed.

    Carries whether the failure is worth retrying, so the replay runner does not
    have to pattern-match on provider-specific error text.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable
        self.status_code = status_code

    def __str__(self) -> str:
        base = super().__str__()
        if self.status_code is None:
            return f"[{self.provider}] {base}"
        return f"[{self.provider}] HTTP {self.status_code}: {base}"


class SecurityLimitExceeded(ParityError):
    """Untrusted input exceeded a configured resource limit.

    Raised instead of attempting the allocation, so a hostile or corrupt file
    cannot exhaust memory.
    """

    def __init__(self, limit_name: str, *, limit: int, actual: int | str) -> None:
        super().__init__(f"security limit '{limit_name}' exceeded: limit={limit}, actual={actual}")
        self.limit_name = limit_name
        self.limit = limit
        self.actual = actual


class CheckError(ParityError):
    """A check could not be evaluated.

    Distinct from a check *failing*: a failure is a finding about the model, an
    error is a defect in the check or its configuration.
    """


class JudgeError(ParityError):
    """A semantic judge could not produce a verdict."""
