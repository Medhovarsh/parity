"""LLM-backed semantic judge.

Consulted only for cases where the deterministic checks passed and the outputs
still differ. Its single question is narrow — *does this difference change what
the output means for a consumer of it?* — because narrow questions are the ones
judges answer reliably.

Three properties matter more than judge quality here:

* **It abstains.** Anything other than a well-formed verdict yields ``None``,
  which surfaces as ``UNVERIFIED``. A judge that guesses is worse than no judge,
  because it converts "unknown" into false confidence.
* **It is deterministic where it can be.** Temperature 0, fixed prompt, seed
  passed through when the provider honours one.
* **It runs anywhere.** It talks to the provider port, so a local Ollama model
  is a first-class judge and no payload has to leave the machine.
"""

from __future__ import annotations

import json
from typing import Any

from parity.checks.base import try_parse_json
from parity.domain.models import (
    Case,
    GenerationParams,
    InteractionInput,
    InteractionOutput,
    JudgeVerdict,
    Message,
)
from parity.errors import JudgeError, ProviderError
from parity.ports.provider import LLMProvider

SYSTEM_PROMPT = """\
You compare two outputs produced by different language models for the same input.

Decide whether the difference between them matters to a program or person \
consuming the output.

Answer with one verdict:
- "equivalent": same meaning, same usable content. Wording, ordering, or \
formatting differences only.
- "acceptable": the content differs, but the candidate still correctly and \
completely serves the request. A different-but-valid answer.
- "broken": the candidate loses information, contradicts the reference, answers \
a different question, is incorrect, or would break a consumer expecting the \
reference.

Reply with JSON only, no prose, no code fence:
{"verdict": "equivalent" | "acceptable" | "broken", "confidence": 0.0-1.0, \
"rationale": "one sentence"}

If you cannot tell, reply with:
{"verdict": "broken", "confidence": 0.0, "rationale": "insufficient information"}
"""

USER_TEMPLATE = """\
<request>
{request}
</request>

<reference_output>
{reference}
</reference_output>

<candidate_output>
{candidate}
</candidate_output>
"""

#: Cap on how much of any single field is sent to the judge. Keeps judging cost
#: bounded and predictable on long outputs.
MAX_FIELD_CHARS = 6000

#: Below this confidence the judge is treated as having abstained. A hedged
#: verdict is not evidence.
MIN_CONFIDENCE = 0.34


def _truncate(text: str, limit: int = MAX_FIELD_CHARS) -> str:
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return f"{text[:limit]}\n…[{omitted} characters omitted]"


def _render_output(output: InteractionOutput) -> str:
    parts: list[str] = []
    if output.text.strip():
        parts.append(output.text)
    for call in output.tool_calls:
        parts.append(f"[tool call] {call.name}({json.dumps(call.arguments, sort_keys=True)})")
    return _truncate("\n".join(parts)) if parts else "(empty output)"


def _render_request(case: Case) -> str:
    lines = [f"{m.role}: {m.content}" for m in case.input.messages if m.content.strip()]
    return _truncate("\n".join(lines))


class LLMJudge:
    """Semantic judge backed by any :class:`~parity.ports.provider.LLMProvider`."""

    def __init__(
        self,
        provider: LLMProvider,
        model: str,
        *,
        min_confidence: float = MIN_CONFIDENCE,
        max_tokens: int = 400,
        seed: int | None = 7,
    ) -> None:
        self._provider = provider
        self._model = model
        self._min_confidence = min_confidence
        self._max_tokens = max_tokens
        self._seed = seed

    @property
    def name(self) -> str:
        return f"llm:{self._provider.name}:{self._model}"

    def _build_request(self, case: Case, candidate: InteractionOutput) -> InteractionInput:
        return InteractionInput(
            messages=(
                Message(role="system", content=SYSTEM_PROMPT),
                Message(
                    role="user",
                    content=USER_TEMPLATE.format(
                        request=_render_request(case),
                        reference=_render_output(case.output),
                        candidate=_render_output(candidate),
                    ),
                ),
            ),
            params=GenerationParams(
                temperature=0.0,
                max_tokens=self._max_tokens,
                seed=self._seed,
            ),
            response_format={"type": "json_object"},
        )

    @staticmethod
    def _parse(text: str) -> JudgeVerdict | None:
        ok, payload = try_parse_json(text)
        if not ok or not isinstance(payload, dict):
            return None
        verdict = payload.get("verdict")
        if verdict not in ("equivalent", "acceptable", "broken"):
            return None

        raw_confidence: Any = payload.get("confidence", 1.0)
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            confidence = 1.0
        confidence = min(1.0, max(0.0, confidence))

        rationale = payload.get("rationale")
        return JudgeVerdict(
            verdict=verdict,
            confidence=confidence,
            rationale=str(rationale)[:500] if rationale is not None else "",
        )

    def compare(self, case: Case, candidate: InteractionOutput) -> JudgeVerdict | None:
        try:
            response = self._provider.complete(self._model, self._build_request(case, candidate))
        except ProviderError as exc:
            raise JudgeError(f"judge provider failed: {exc}") from exc

        opinion = self._parse(response.text)
        if opinion is None:
            return None
        if opinion.confidence < self._min_confidence:
            # A judge that is unsure has told us nothing. Abstain rather than
            # let a low-confidence "broken" block a deploy.
            return None
        return opinion

    def close(self) -> None:
        self._provider.close()
