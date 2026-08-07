"""Application service layer.

Wires configuration to concrete adapters. This is the composition root: the one
place that knows about both :mod:`parity.config` and :mod:`parity.adapters`.

Kept separate from the CLI so an application can embed the same behaviour
without shelling out, and so the wiring is testable without invoking a command.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from parity.adapters.judges.llm_judge import LLMJudge
from parity.adapters.judges.none_judge import NoJudge
from parity.adapters.providers.registry import build_provider
from parity.adapters.stores.registry import open_baseline_store
from parity.adapters.stores.run_store import FileRunStore
from parity.checks.registry import build_pipeline
from parity.classify.classifier import Classifier
from parity.config import ParityConfig
from parity.domain.models import ModelRef
from parity.errors import ConfigError
from parity.ports.judge import SemanticJudge
from parity.ports.provider import LLMProvider
from parity.ports.store import BaselineStore
from parity.replay.runner import ReplayRunner
from parity.security.limits import Limits
from parity.security.redaction import Redactor


class Application:
    """Builds the objects a command needs from a loaded configuration."""

    def __init__(self, config: ParityConfig) -> None:
        self.config = config

    # -- storage ---------------------------------------------------------

    @property
    def limits(self) -> Limits:
        return self.config.security.limits

    def open_baseline(self, path: Path | None = None) -> BaselineStore:
        return open_baseline_store(
            self.config.baseline.store,
            path or self.config.baseline_path(),
            limits=self.limits,
        )

    def open_runs(self) -> FileRunStore:
        return FileRunStore(self.config.runs_path(), limits=self.limits)

    def redactor(self) -> Redactor | None:
        """``None`` when redaction is disabled, which must be deliberate."""
        if not self.config.security.redact:
            return None
        return Redactor(categories=frozenset(self.config.security.categories))

    # -- models ----------------------------------------------------------

    def provider_for(self, ref: ModelRef) -> LLMProvider:
        """Build the provider named by ``ref.provider``.

        The name is a key into the ``providers`` table, not a hardcoded vendor —
        which is what lets ``parity replay --candidate staging:my-model`` work.
        """
        return build_provider(self.config.provider(ref.provider))

    def judge(self) -> SemanticJudge:
        """The configured judge, or :class:`NoJudge` when none is enabled."""
        judge_config = self.config.judge
        if not judge_config.enabled:
            return NoJudge()
        try:
            provider_config = self.config.provider(judge_config.provider)
        except ConfigError as exc:
            raise ConfigError(f"judge is enabled but its provider is unusable: {exc}") from exc
        return LLMJudge(
            build_provider(provider_config),
            judge_config.model,
            min_confidence=judge_config.min_confidence,
            max_tokens=judge_config.max_tokens,
        )

    def classifier(self, judge: SemanticJudge | None = None) -> Classifier:
        settings = self.config.checks
        resolved = judge if judge is not None else self.judge()
        # NoJudge and None are equivalent to the classifier; pass None so the
        # judge branch is skipped entirely rather than round-tripping abstentions.
        effective = None if isinstance(resolved, NoJudge) else resolved
        return Classifier(
            pipeline=build_pipeline(settings),
            settings=settings,
            judge=effective,
        )

    def runner(
        self,
        candidate: ModelRef,
        *,
        provider: LLMProvider | None = None,
        classifier: Classifier | None = None,
    ) -> ReplayRunner:
        replay = self.config.replay
        return ReplayRunner(
            provider=provider or self.provider_for(candidate),
            model=candidate.model,
            classifier=classifier or self.classifier(),
            concurrency=replay.concurrency,
            max_retries=replay.max_retries,
            base_delay_seconds=replay.retry_base_delay_seconds,
            max_delay_seconds=replay.retry_max_delay_seconds,
        )

    @contextmanager
    def replay_session(self, candidate: ModelRef) -> Iterator[ReplayRunner]:
        """Runner with provider and judge lifetimes managed.

        Both are closed on the way out, including on the error path — a leaked
        HTTP client keeps a process alive after the CLI has printed its report.
        """
        provider = self.provider_for(candidate)
        judge = self.judge()
        try:
            yield self.runner(
                candidate,
                provider=provider,
                classifier=self.classifier(judge),
            )
        finally:
            judge.close()
            provider.close()
