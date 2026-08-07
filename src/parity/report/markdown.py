"""Markdown report, sized for a pull-request comment.

Reviewers skim. The verdict line and the counts go at the top; the detail sits
inside a collapsed ``<details>`` block so a passing run occupies four lines of a
PR thread rather than four screens.
"""

from __future__ import annotations

from parity.domain.models import RunReport, Verdict
from parity.gate import GateDecision

VERDICT_ICON: dict[Verdict, str] = {
    Verdict.EQUIVALENT: "✅",
    Verdict.ACCEPTABLE: "🟦",
    Verdict.UNVERIFIED: "🟨",
    Verdict.BROKEN: "❌",
    Verdict.ERROR: "🟥",
}

MAX_DETAIL_ROWS = 50


def _escape(text: str) -> str:
    """Neutralise pipes and newlines so a message cannot break the table."""
    return text.replace("|", "\\|").replace("\n", " ").strip()


def render_markdown(report: RunReport, decision: GateDecision | None = None) -> str:
    summary = report.summary
    lines: list[str] = []

    if decision is not None:
        headline = "**Parity: passed**" if decision.passed else "**Parity: failed**"
        icon = "✅" if decision.passed else "❌"
        lines.append(f"{icon} {headline} — {_escape(decision.summary())}")
    else:
        lines.append("**Parity replay**")

    lines.append("")
    lines.append(f"`{report.baseline_ref}` → `{report.candidate_ref}` · {summary.total} case(s)")
    lines.append("")
    lines.append("| verdict | cases |")
    lines.append("| --- | ---: |")
    for verdict, count in (
        (Verdict.EQUIVALENT, summary.equivalent),
        (Verdict.ACCEPTABLE, summary.acceptable),
        (Verdict.UNVERIFIED, summary.unverified),
        (Verdict.BROKEN, summary.broken),
        (Verdict.ERROR, summary.error),
    ):
        lines.append(f"| {VERDICT_ICON[verdict]} {verdict.value} | {count} |")

    interesting = report.outcomes_with(Verdict.BROKEN, Verdict.ERROR, Verdict.UNVERIFIED)
    if interesting:
        order = {Verdict.ERROR: 0, Verdict.BROKEN: 1, Verdict.UNVERIFIED: 2}
        ranked = sorted(interesting, key=lambda o: (order.get(o.verdict, 9), o.case_id))
        lines.append("")
        lines.append(f"<details><summary>{len(interesting)} case(s) needing review</summary>")
        lines.append("")
        lines.append("| case | verdict | check | what happened |")
        lines.append("| --- | --- | --- | --- |")
        for outcome in ranked[:MAX_DETAIL_ROWS]:
            blocking = [c for c in outcome.checks if c.blocking_failure]
            failed = blocking or list(outcome.failed_checks)
            check = failed[0].check if failed else ("provider" if outcome.error else "—")
            reason = outcome.primary_reason or "outputs differ; no judge configured"
            lines.append(
                f"| `{outcome.case_id[:12]}` | {VERDICT_ICON[outcome.verdict]} "
                f"{outcome.verdict.value} | `{check}` | {_escape(reason)} |"
            )
        if len(ranked) > MAX_DETAIL_ROWS:
            lines.append("")
            lines.append(f"_…and {len(ranked) - MAX_DETAIL_ROWS} more._")
        lines.append("")
        lines.append("</details>")

    lines.append("")
    lines.append(
        f"<sub>parity {report.parity_version} · judge `{report.judge}` · "
        f"run `{report.run_id}`</sub>"
    )
    return "\n".join(lines) + "\n"
