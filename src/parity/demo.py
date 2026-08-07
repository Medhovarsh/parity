"""A complete, self-contained demonstration run.

Adoption dies at "I'd have to assemble a log file first". ``parity demo`` removes
that: it synthesises a baseline, replays it against a scripted offline provider,
and shows a real report in seconds — no config, no credentials, no network, no
files written unless asked.

Every regression class the tool detects appears exactly once, so the output
doubles as documentation of what the checks actually catch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from parity.adapters.providers.fake import FakeProvider
from parity.domain.models import (
    Case,
    GenerationParams,
    InteractionInput,
    InteractionOutput,
    Message,
    ModelRef,
    ToolCall,
)

DEMO_BASELINE_REF = ModelRef(provider="demo", model="gpt-4o-mini")
DEMO_CANDIDATE_REF = ModelRef(provider="demo", model="gpt-5-mini")

SEARCH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "search_orders",
        "description": "Search a customer's orders",
        "parameters": {
            "type": "object",
            "properties": {"customer_id": {"type": "string"}, "period": {"type": "string"}},
        },
    },
}


@dataclass(frozen=True)
class DemoCase:
    """One scripted scenario: the baseline behaviour and what the candidate does."""

    label: str
    teaches: str
    prompt: str
    baseline: InteractionOutput
    candidate: InteractionOutput
    system: str | None = None
    tools: tuple[dict[str, Any], ...] = ()


def _json_out(payload: dict[str, Any], model: str) -> InteractionOutput:
    return InteractionOutput(text=json.dumps(payload, indent=2), finish_reason="stop", model=model)


def _text_out(text: str, model: str, finish_reason: str = "stop") -> InteractionOutput:
    return InteractionOutput(text=text, finish_reason=finish_reason, model=model)


OLD = DEMO_BASELINE_REF.model
NEW = DEMO_CANDIDATE_REF.model

DEMO_CASES: tuple[DemoCase, ...] = (
    DemoCase(
        label="invoice-stable",
        teaches="unchanged behaviour is reported as equivalent and needs no attention",
        system="Extract invoice fields. Reply with JSON only.",
        prompt=(
            "Invoice 4417 from Acme Ltd, 2026-03-11. Subtotal 1200.00, tax 240.00, total 1440.00."
        ),
        baseline=_json_out(
            {
                "invoice_number": "4417",
                "vendor": "Acme Ltd",
                "subtotal": 1200.0,
                "tax_total": 240.0,
                "total": 1440.0,
            },
            OLD,
        ),
        candidate=_json_out(
            {
                "invoice_number": "4417",
                "vendor": "Acme Ltd",
                "subtotal": 1200.0,
                "tax_total": 240.0,
                "total": 1440.0,
            },
            NEW,
        ),
    ),
    DemoCase(
        label="invoice-dropped-field",
        teaches="the new model silently stops emitting a field a consumer depends on",
        system="Extract invoice fields. Reply with JSON only.",
        prompt="Invoice 4418 from Globex, 2026-03-12. Subtotal 90.50, tax 18.10, total 108.60.",
        baseline=_json_out(
            {
                "invoice_number": "4418",
                "vendor": "Globex",
                "subtotal": 90.5,
                "tax_total": 18.1,
                "total": 108.6,
            },
            OLD,
        ),
        candidate=_json_out(
            {"invoice_number": "4418", "vendor": "Globex", "subtotal": 90.5, "total": 108.6},
            NEW,
        ),
    ),
    DemoCase(
        label="agent-stopped-calling-tool",
        teaches=(
            "the new model answers from memory instead of calling the tool; "
            "nothing errors, the answer is just ungrounded"
        ),
        prompt="Find orders placed by customer 88213 last month.",
        tools=(SEARCH_TOOL,),
        baseline=InteractionOutput(
            text="",
            tool_calls=(
                ToolCall(
                    name="search_orders",
                    arguments={"customer_id": "88213", "period": "last_month"},
                    call_id="call_1",
                ),
            ),
            finish_reason="tool_calls",
            model=OLD,
        ),
        candidate=_text_out(
            "Customer 88213 placed three orders last month, totalling about $420.", NEW
        ),
    ),
    DemoCase(
        label="output-stopped-parsing",
        teaches="JSON output degrades into prose and every downstream parser breaks",
        system="Classify sentiment. Reply with JSON only.",
        prompt="Classify: 'Shipping took three weeks but the product is excellent.'",
        baseline=_json_out({"sentiment": "mixed", "confidence": 0.82}, OLD),
        candidate=_text_out(
            "The sentiment here is mixed — the shipping experience was negative "
            "while the product itself was praised.",
            NEW,
        ),
    ),
    DemoCase(
        label="new-refusal",
        teaches="a prompt that worked for months starts getting declined",
        prompt="Draft a firm final notice for an unpaid invoice, 60 days overdue.",
        baseline=_text_out(
            "FINAL NOTICE\n\nOur records show invoice 4417 remains unpaid 60 days "
            "past its due date. Payment is required within 7 days to avoid escalation.",
            OLD,
        ),
        candidate=_text_out(
            "I'm sorry, but I can't help with drafting debt collection communications.", NEW
        ),
    ),
    DemoCase(
        label="truncated-output",
        teaches=(
            "generation is cut off mid-sentence and the result looks plausible until you read it"
        ),
        prompt="Summarise our Q1 refund policy changes in three bullet points.",
        baseline=_text_out(
            "- Refund window extended from 14 to 30 days.\n"
            "- Restocking fee removed for unopened items.\n"
            "- Store credit now issued instantly on approval.",
            OLD,
        ),
        candidate=_text_out(
            "- Refund window extended from 14 to 30 days.\n- Restocking fee remo",
            NEW,
            finish_reason="length",
        ),
    ),
    DemoCase(
        label="numbers-moved",
        teaches="structure holds but a value shifted — visible as a field diff, not a wall of text",
        system="Score the risk. Reply with JSON only.",
        prompt="Assess refund risk for order 5521: two prior chargebacks, new address.",
        baseline=_json_out({"risk": "high", "score": 0.88, "reasons": ["chargebacks"]}, OLD),
        candidate=_json_out({"risk": "medium", "score": 0.41, "reasons": ["chargebacks"]}, NEW),
    ),
    DemoCase(
        label="reworded-prose",
        teaches=(
            "wording changed with no structural signal; nothing can call this good "
            "or bad without a judge, so it is reported honestly as unverified"
        ),
        prompt=(
            "Summarise this support thread: customer could not log in, reset password, resolved."
        ),
        baseline=_text_out(
            "The customer was unable to log in to their account. After resetting "
            "their password, the issue was resolved.",
            OLD,
        ),
        candidate=_text_out(
            "A customer reported being locked out of their account. Resetting the "
            "password fixed the problem.",
            NEW,
        ),
    ),
)


def build_case(demo: DemoCase) -> Case:
    messages: list[Message] = []
    if demo.system:
        messages.append(Message(role="system", content=demo.system))
    messages.append(Message(role="user", content=demo.prompt))
    return Case.create(
        input=InteractionInput(
            messages=tuple(messages),
            params=GenerationParams(temperature=0.0),
            tools=demo.tools,
        ),
        output=demo.baseline,
        reference=DEMO_BASELINE_REF,
        tags=(demo.label,),
    )


def build_demo() -> tuple[list[Case], FakeProvider, dict[str, str]]:
    """Build the demo baseline, a provider scripted to produce the candidate
    outputs, and a map from case id to the lesson that case teaches.
    """
    cases: list[Case] = []
    scripted: dict[str, InteractionOutput] = {}
    lessons: dict[str, str] = {}

    for demo in DEMO_CASES:
        case = build_case(demo)
        cases.append(case)
        scripted[case.input.fingerprint()] = demo.candidate
        lessons[case.case_id] = demo.teaches

    return cases, FakeProvider(name="demo", scripted=scripted), lessons
