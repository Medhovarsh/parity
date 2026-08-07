"""The replay runner.

Executes every baseline case against a candidate model and classifies the
result. Three properties are non-negotiable here:

* **One case cannot fail the run.** A provider error, a malformed response, or a
  bug in a check produces an ``ERROR`` verdict for that case and the run
  continues. A migration review is useless if it aborts at case 40 of 800.
* **Output order matches baseline order.** Concurrency is an implementation
  detail; a report that reshuffles itself between runs cannot be diffed.
* **Retries are bounded and only for retryable failures.** The provider decides
  what is retryable, and backoff is exponential with jitter to avoid
  synchronising a thread pool against a rate limiter.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from parity.adapters.clock import SystemClock
from parity.classify.classifier import Classifier
from parity.domain.models import Case, CaseOutcome, ModelRef, RunReport, Verdict
from parity.errors import ProviderError
from parity.ports.clock import Clock
from parity.ports.provider import LLMProvider


@dataclass(frozen=True)
class RunProgress:
    """Emitted after each case completes, for progress display."""

    completed: int
    total: int
    case_id: str
    verdict: Verdict


ProgressCallback = Callable[[RunProgress], None]


class ReplayRunner:
    """Replays cases against a candidate provider and classifies the results."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        model: str,
        classifier: Classifier,
        concurrency: int = 4,
        max_retries: int = 2,
        base_delay_seconds: float = 1.0,
        max_delay_seconds: float = 30.0,
        clock: Clock | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._classifier = classifier
        self._concurrency = max(1, concurrency)
        self._max_retries = max(0, max_retries)
        self._base_delay = max(0.0, base_delay_seconds)
        self._max_delay = max(0.0, max_delay_seconds)
        self._clock: Clock = clock or SystemClock()
        # Seeded so a run's backoff pattern is reproducible when a fake clock
        # is supplied; unseeded randomness would make retry tests flaky.
        self._rng = rng or random.Random(0x9E3779B9)  # noqa: S311 - jitter, not crypto

    @property
    def candidate_ref(self) -> ModelRef:
        return ModelRef(provider=self._provider.name, model=self._model)

    # -- single case -----------------------------------------------------

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped."""
        ceiling = min(self._max_delay, self._base_delay * (2**attempt))
        return self._rng.uniform(0.0, ceiling) if ceiling > 0 else 0.0

    def replay_case(self, case: Case) -> CaseOutcome:
        """Replay and classify one case. Never raises."""
        started = time.perf_counter()
        last_error: str | None = None

        for attempt in range(self._max_retries + 1):
            try:
                candidate = self._provider.complete(self._model, case.input)
            except ProviderError as exc:
                last_error = str(exc)
                if not exc.retryable or attempt == self._max_retries:
                    break
                self._clock.sleep(self._backoff_delay(attempt))
                continue
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                break
            else:
                elapsed = int((time.perf_counter() - started) * 1000)
                return self._classifier.classify_to_outcome(case, candidate, duration_ms=elapsed)

        return CaseOutcome(
            case_id=case.case_id,
            verdict=Verdict.ERROR,
            error=last_error or "provider produced no response",
            duration_ms=int((time.perf_counter() - started) * 1000),
            tags=case.tags,
        )

    # -- whole run -------------------------------------------------------

    def run(
        self,
        cases: Iterable[Case],
        *,
        on_progress: ProgressCallback | None = None,
    ) -> tuple[CaseOutcome, ...]:
        """Replay every case, preserving input order in the results."""
        ordered: Sequence[Case] = list(cases)
        if not ordered:
            return ()

        if self._concurrency == 1:
            return tuple(self._run_serial(ordered, on_progress))
        return tuple(self._run_concurrent(ordered, on_progress))

    def _run_serial(
        self, cases: Sequence[Case], on_progress: ProgressCallback | None
    ) -> list[CaseOutcome]:
        outcomes: list[CaseOutcome] = []
        for index, case in enumerate(cases, start=1):
            outcome = self.replay_case(case)
            outcomes.append(outcome)
            self._emit(on_progress, index, len(cases), outcome)
        return outcomes

    def _run_concurrent(
        self, cases: Sequence[Case], on_progress: ProgressCallback | None
    ) -> list[CaseOutcome]:
        results: list[CaseOutcome | None] = [None] * len(cases)
        completed = 0
        with ThreadPoolExecutor(
            max_workers=self._concurrency, thread_name_prefix="parity-replay"
        ) as pool:
            futures: dict[Future[CaseOutcome], int] = {
                pool.submit(self.replay_case, case): index for index, case in enumerate(cases)
            }
            for future in as_completed(futures):
                index = futures[future]
                outcome = future.result()  # replay_case never raises
                results[index] = outcome
                completed += 1
                self._emit(on_progress, completed, len(cases), outcome)
        # Every slot is filled: replay_case always returns an outcome.
        return [outcome for outcome in results if outcome is not None]

    @staticmethod
    def _emit(
        on_progress: ProgressCallback | None,
        completed: int,
        total: int,
        outcome: CaseOutcome,
    ) -> None:
        if on_progress is None:
            return
        on_progress(
            RunProgress(
                completed=completed,
                total=total,
                case_id=outcome.case_id,
                verdict=outcome.verdict,
            )
        )

    # -- report ----------------------------------------------------------

    def build_report(
        self,
        *,
        parity_version: str,
        baseline_ref: ModelRef,
        baseline_source: str,
        outcomes: tuple[CaseOutcome, ...],
        config_snapshot: dict[str, object] | None = None,
    ) -> RunReport:
        return RunReport.build(
            parity_version=parity_version,
            baseline_ref=baseline_ref,
            candidate_ref=self.candidate_ref,
            baseline_source=baseline_source,
            judge=self._classifier.judge_name,
            outcomes=outcomes,
            config_snapshot=config_snapshot or {},
            created_at=self._clock.now(),
        )
