"""Self-contained HTML report.

The artefact people actually share. One file, no external stylesheet, no script
from a CDN, no font download — it opens from a ticket attachment, an S3 bucket,
or a CI artefact on a machine with no network, and it renders identically.

Everything is escaped. Model output is untrusted text and this file gets opened
in a browser, so an unescaped `<script>` in a completion would be a stored XSS
delivered by the tool meant to catch problems.
"""

from __future__ import annotations

from html import escape

from parity.diff import OutputDiff
from parity.domain.models import CaseOutcome, RunReport, Verdict
from parity.gate import GateDecision

VERDICT_ORDER = (
    Verdict.ERROR,
    Verdict.BROKEN,
    Verdict.UNVERIFIED,
    Verdict.ACCEPTABLE,
    Verdict.EQUIVALENT,
)

_STYLE = """\
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #1a1d21; --muted: #6b7280; --line: #e5e7eb;
  --card: #f9fafb; --code: #f3f4f6;
  --equivalent: #16a34a; --acceptable: #0891b2; --unverified: #ca8a04;
  --broken: #dc2626; --error: #9333ea;
  --add: #166534; --add-bg: #dcfce7; --del: #991b1b; --del-bg: #fee2e2;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --fg: #e6edf3; --muted: #8b949e; --line: #30363d;
    --card: #161b22; --code: #1c2128;
    --equivalent: #3fb950; --acceptable: #39c5cf; --unverified: #d29922;
    --broken: #f85149; --error: #bc8cff;
    --add: #7ee787; --add-bg: #12261e; --del: #ffa198; --del-bg: #25171c;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem 1.25rem 4rem; background: var(--bg); color: var(--fg);
  font: 15px/1.6 ui-sans-serif, -apple-system, "Segoe UI", Roboto, sans-serif;
}
.wrap { max-width: 62rem; margin: 0 auto; }
h1 { font-size: 1.5rem; margin: 0 0 .25rem; letter-spacing: -0.01em; }
h2 { font-size: 1.05rem; margin: 2.5rem 0 .75rem; }
.sub { color: var(--muted); font-size: .875rem; margin: 0 0 1.5rem; }
code, pre, .mono { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
.banner {
  padding: .75rem 1rem; border-radius: 8px; font-weight: 600; margin-bottom: 1.5rem;
  border: 1px solid var(--line);
}
.banner.pass { color: var(--equivalent); }
.banner.fail { color: var(--broken); }
.counts { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: 1rem; }
.count {
  border: 1px solid var(--line); border-radius: 8px; padding: .6rem .9rem;
  background: var(--card); min-width: 7.5rem;
}
.count .n { font-size: 1.4rem; font-weight: 650; line-height: 1.2; }
.count .l { font-size: .75rem; color: var(--muted); text-transform: uppercase;
  letter-spacing: .04em; }
.case {
  border: 1px solid var(--line); border-radius: 8px; margin-bottom: .75rem;
  background: var(--card); overflow: hidden;
}
.case > summary {
  cursor: pointer; padding: .7rem .9rem; display: flex; gap: .75rem;
  align-items: baseline; flex-wrap: wrap; list-style: none;
}
.case > summary::-webkit-details-marker { display: none; }
.case > summary::before { content: "▸"; color: var(--muted); }
.case[open] > summary::before { content: "▾"; }
.pill {
  font-size: .7rem; font-weight: 700; text-transform: uppercase;
  letter-spacing: .04em; padding: .12rem .45rem; border-radius: 999px;
  border: 1px solid currentColor;
}
.id { font-size: .8rem; color: var(--muted); }
.why { flex: 1 1 18rem; font-size: .875rem; }
.body { padding: 0 .9rem .9rem; border-top: 1px solid var(--line); }
.label { font-size: .7rem; text-transform: uppercase; letter-spacing: .05em;
  color: var(--muted); margin: 1rem 0 .35rem; }
pre {
  background: var(--code); border: 1px solid var(--line); border-radius: 6px;
  padding: .7rem .8rem; overflow-x: auto; font-size: .8rem; margin: 0;
  white-space: pre-wrap; word-break: break-word;
}
.diff { font-size: .8rem; }
.diff div { padding: .1rem .4rem; border-radius: 4px; white-space: pre-wrap;
  word-break: break-word; }
.diff .a { color: var(--add); background: var(--add-bg); }
.diff .d { color: var(--del); background: var(--del-bg); }
.diff .c { color: var(--unverified); }
table { border-collapse: collapse; width: 100%; font-size: .85rem; }
th, td { text-align: left; padding: .4rem .6rem; border-bottom: 1px solid var(--line); }
th { color: var(--muted); font-weight: 600; font-size: .75rem;
  text-transform: uppercase; letter-spacing: .04em; }
footer { margin-top: 3rem; color: var(--muted); font-size: .8rem;
  border-top: 1px solid var(--line); padding-top: 1rem; }
.empty { color: var(--muted); font-style: italic; }
"""


def _verdict_style(verdict: Verdict) -> str:
    return f"color: var(--{verdict.value})"


def _render_diff(diff: OutputDiff) -> str:
    """Field-level changes when structured, unified text diff otherwise."""
    rows: list[str] = []

    css_for = {"removed": "d", "added": "a", "changed": "c"}

    for tool_change in diff.tool_calls:
        rows.append(
            f'<div class="{css_for[tool_change.kind.value]}">{escape(tool_change.render())}</div>'
        )

    if diff.structured:
        for field_change in diff.fields:
            rows.append(
                f'<div class="{css_for[field_change.kind.value]}">'
                f"{escape(field_change.render())}</div>"
            )
    else:
        for line in diff.text_unified():
            if line.startswith(("---", "+++", "@@")):
                continue
            css = "a" if line.startswith("+") else "d" if line.startswith("-") else ""
            rows.append(f'<div class="{css}">{escape(line)}</div>')

    if diff.finish_reason_changed:
        rows.append(
            '<div class="c">'
            + escape(
                f"~ finish reason: {diff.finish_reason_before or 'none'}"
                f" → {diff.finish_reason_after or 'none'}"
            )
            + "</div>"
        )

    if not rows:
        return '<p class="empty">No difference.</p>'
    return f'<div class="diff">{"".join(rows)}</div>'


def _render_case(outcome: CaseOutcome, baseline_text: str | None) -> str:
    parts: list[str] = [
        "<details class='case'>",
        "<summary>",
        f"<span class='pill' style='{_verdict_style(outcome.verdict)}'>"
        f"{escape(outcome.verdict.value)}</span>",
        f"<span class='id mono'>{escape(outcome.case_id[:16])}</span>",
        f"<span class='why'>{escape(outcome.primary_reason or 'outputs differ')}</span>",
        "</summary>",
        "<div class='body'>",
    ]

    if outcome.error:
        parts.append("<div class='label'>Error</div>")
        parts.append(f"<pre>{escape(outcome.error)}</pre>")

    failed = [c for c in outcome.checks if c.status.value == "fail"]
    if failed:
        parts.append("<div class='label'>Failing checks</div>")
        parts.append("<table><tr><th>check</th><th>severity</th><th>detail</th></tr>")
        for check in failed:
            parts.append(
                f"<tr><td class='mono'>{escape(check.check)}</td>"
                f"<td>{escape(check.severity.value)}</td>"
                f"<td>{escape(check.message)}</td></tr>"
            )
        parts.append("</table>")

    if outcome.judge_rationale:
        parts.append("<div class='label'>Judge</div>")
        parts.append(f"<pre>{escape(outcome.judge_rationale)}</pre>")

    if baseline_text is not None and outcome.candidate is not None:
        parts.append("<div class='label'>Baseline output</div>")
        parts.append(f"<pre>{escape(baseline_text[:4000]) or '(empty)'}</pre>")
        parts.append("<div class='label'>Candidate output</div>")
        parts.append(f"<pre>{escape(outcome.candidate.text[:4000]) or '(empty)'}</pre>")

    parts.append("</div></details>")
    return "".join(parts)


def render_html(
    report: RunReport,
    decision: GateDecision | None = None,
    *,
    baselines: dict[str, str] | None = None,
    diffs: dict[str, OutputDiff] | None = None,
) -> str:
    """Render a run report as one self-contained HTML document.

    ``baselines`` maps case id to the reference output text, and ``diffs`` to a
    precomputed diff. Both are optional — the report degrades to check results
    and candidate output when the baseline is not available to the caller.
    """
    baselines = baselines or {}
    diffs = diffs or {}
    summary = report.summary

    head = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width,initial-scale=1'>",
        f"<title>Parity — {escape(str(report.candidate_ref))}</title>",
        f"<style>{_STYLE}</style></head><body><div class='wrap'>",
    ]

    body: list[str] = [
        "<h1>Parity behaviour report</h1>",
        f"<p class='sub mono'>{escape(str(report.baseline_ref))} &rarr; "
        f"{escape(str(report.candidate_ref))} &middot; {summary.total} case(s) &middot; "
        f"judge {escape(report.judge)} &middot; {escape(report.created_at.isoformat())}</p>",
    ]

    if decision is not None:
        css = "pass" if decision.passed else "fail"
        body.append(f"<div class='banner {css}'>{escape(decision.summary())}</div>")

    body.append("<div class='counts'>")
    for verdict, count in (
        (Verdict.EQUIVALENT, summary.equivalent),
        (Verdict.ACCEPTABLE, summary.acceptable),
        (Verdict.UNVERIFIED, summary.unverified),
        (Verdict.BROKEN, summary.broken),
        (Verdict.ERROR, summary.error),
    ):
        body.append(
            f"<div class='count'><div class='n' style='{_verdict_style(verdict)}'>{count}</div>"
            f"<div class='l'>{escape(verdict.value)}</div></div>"
        )
    body.append("</div>")

    interesting = report.outcomes_with(Verdict.BROKEN, Verdict.ERROR, Verdict.UNVERIFIED)
    if interesting:
        rank = {v: i for i, v in enumerate(VERDICT_ORDER)}
        body.append(f"<h2>{len(interesting)} case(s) needing review</h2>")
        for outcome in sorted(interesting, key=lambda o: (rank.get(o.verdict, 9), o.case_id)):
            body.append(_render_case(outcome, baselines.get(outcome.case_id)))
            diff = diffs.get(outcome.case_id)
            if diff is not None:
                body.append(
                    "<div class='case'><div class='body'>"
                    "<div class='label'>What changed</div>" + _render_diff(diff) + "</div></div>"
                )
    else:
        body.append("<h2>No cases need review</h2>")
        body.append("<p class='empty'>Every case was equivalent or judged acceptable.</p>")

    body.append(
        f"<footer>Generated by Parity {escape(report.parity_version)} &middot; "
        f"run <span class='mono'>{escape(report.run_id)}</span> &middot; "
        f"baseline <span class='mono'>{escape(report.baseline_source)}</span></footer>"
    )

    return "".join(head) + "".join(body) + "</div></body></html>\n"


def render_html_from_report(report: RunReport, decision: GateDecision | None = None) -> str:
    """Convenience wrapper that derives diffs from the report alone.

    The report stores candidate outputs but not baseline ones, so diffs are only
    produced when a caller supplies the baseline. This path still yields a useful
    document: verdicts, failing checks, and candidate output.
    """
    return render_html(report, decision)
