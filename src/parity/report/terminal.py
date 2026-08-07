"""Terminal report.

Optimised for the moment it exists to serve: an engineer has replayed 800 cases
and needs to know, in five seconds, whether to ship. So the summary comes first,
then only the cases that need attention, then how to see the rest.

Passing cases are not listed. A report that scrolls a wall of green trains people
to stop reading it.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from parity.domain.models import RunReport, Verdict
from parity.gate import GateDecision

VERDICT_STYLE: dict[Verdict, str] = {
    Verdict.EQUIVALENT: "green",
    Verdict.ACCEPTABLE: "cyan",
    Verdict.UNVERIFIED: "yellow",
    Verdict.BROKEN: "red",
    Verdict.ERROR: "magenta",
}

VERDICT_LABEL: dict[Verdict, str] = {
    Verdict.EQUIVALENT: "equivalent",
    Verdict.ACCEPTABLE: "acceptable",
    Verdict.UNVERIFIED: "unverified",
    Verdict.BROKEN: "broken",
    Verdict.ERROR: "error",
}

#: Cases shown in full before the report starts pointing at the JSON instead.
MAX_DETAIL_ROWS = 25


def summary_table(report: RunReport) -> Table:
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("verdict", style="bold")
    table.add_column("cases", justify="right")
    table.add_column("meaning")

    rows = (
        (Verdict.EQUIVALENT, report.summary.equivalent, "identical after normalisation"),
        (Verdict.ACCEPTABLE, report.summary.acceptable, "differs, judged still correct"),
        (Verdict.UNVERIFIED, report.summary.unverified, "differs, nothing judged it"),
        (Verdict.BROKEN, report.summary.broken, "structurally or semantically regressed"),
        (Verdict.ERROR, report.summary.error, "could not be replayed"),
    )
    for verdict, count, meaning in rows:
        style = VERDICT_STYLE[verdict] if count else "dim"
        table.add_row(
            Text(VERDICT_LABEL[verdict], style=style),
            Text(str(count), style=style),
            Text(meaning, style="dim"),
        )
    return table


def detail_table(report: RunReport) -> Table | None:
    """Cases that need a human. Returns ``None`` when there are none."""
    interesting = report.outcomes_with(Verdict.BROKEN, Verdict.ERROR, Verdict.UNVERIFIED)
    if not interesting:
        return None

    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    table.add_column("case", style="dim", no_wrap=True)
    table.add_column("verdict", no_wrap=True)
    table.add_column("check", no_wrap=True)
    table.add_column("what happened", overflow="fold")

    # Worst first: a reviewer's attention is finite and should go to breaks.
    order = {Verdict.ERROR: 0, Verdict.BROKEN: 1, Verdict.UNVERIFIED: 2}
    ranked = sorted(interesting, key=lambda o: (order.get(o.verdict, 9), o.case_id))

    for outcome in ranked[:MAX_DETAIL_ROWS]:
        blocking = [c for c in outcome.checks if c.blocking_failure]
        failed = blocking or list(outcome.failed_checks)
        check_name = failed[0].check if failed else ("—" if not outcome.error else "provider")
        table.add_row(
            outcome.case_id[:12],
            Text(VERDICT_LABEL[outcome.verdict], style=VERDICT_STYLE[outcome.verdict]),
            check_name,
            outcome.primary_reason or "outputs differ; no judge configured",
        )
    return table


def render_terminal(
    report: RunReport,
    decision: GateDecision | None = None,
    *,
    console: Console | None = None,
) -> None:
    """Print the report. Writes to stdout unless a console is supplied."""
    out = console or Console()

    header = (
        f"[bold]{report.baseline_ref}[/bold] → [bold]{report.candidate_ref}[/bold]\n"
        f"[dim]{report.summary.total} case(s) · judge: {report.judge} · run {report.run_id}[/dim]"
    )
    out.print(Panel(header, title="parity replay", title_align="left", border_style="dim"))
    out.print(summary_table(report))

    details = detail_table(report)
    if details is not None:
        out.print()
        out.print(details)
        interesting = len(report.outcomes_with(Verdict.BROKEN, Verdict.ERROR, Verdict.UNVERIFIED))
        if interesting > MAX_DETAIL_ROWS:
            out.print(
                f"[dim]…and {interesting - MAX_DETAIL_ROWS} more. "
                f"Run `parity report {report.run_id} --format json` for all of them.[/dim]"
            )

    if decision is not None:
        out.print()
        style = "bold green" if decision.passed else "bold red"
        out.print(Text(decision.summary(), style=style))

    if report.summary.unverified and report.judge == "none":
        out.print(
            "\n[dim]Tip: "
            f"{report.summary.unverified} case(s) changed but nothing compared them "
            "semantically. Enable a judge in parity.toml — a local Ollama model "
            "works and costs nothing.[/dim]"
        )
