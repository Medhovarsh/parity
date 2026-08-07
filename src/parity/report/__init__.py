"""Reporters: rendering a run report for humans and for machines.

Four formats, each with a job:

* ``terminal`` — what a developer reads while migrating.
* ``markdown`` — what gets pasted into a PR comment.
* ``json`` — the machine-readable artefact; the run report itself.
* ``junit`` — what CI systems already know how to display and trend.
"""

from parity.report.junit import render_junit
from parity.report.markdown import render_markdown
from parity.report.terminal import render_terminal

__all__ = ["FORMATS", "render", "render_junit", "render_markdown", "render_terminal"]

FORMATS: tuple[str, ...] = ("terminal", "markdown", "json", "junit")


def render(report: object, fmt: str) -> str:  # pragma: no cover - thin dispatch
    """Render by format name. ``terminal`` is handled by the CLI's console."""
    from parity.domain.models import RunReport

    if not isinstance(report, RunReport):
        raise TypeError("render expects a RunReport")
    if fmt == "markdown":
        return render_markdown(report)
    if fmt == "junit":
        return render_junit(report)
    if fmt == "json":
        return report.model_dump_json(indent=2)
    raise ValueError(f"unknown report format {fmt!r}; expected one of {', '.join(FORMATS)}")
