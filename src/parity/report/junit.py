"""JUnit XML report.

Every CI system already knows how to ingest, display, and trend JUnit XML.
Emitting it means a Parity run shows up in the same place as the unit tests, with
no plugin and no integration work.

Mapping:

* ``broken``     → ``<failure>``  (a real regression)
* ``error``      → ``<error>``    (could not be evaluated)
* ``unverified`` → ``<skipped>``  (changed, nobody judged it)
* everything else → a passing test case

Built with ``xml.etree.ElementTree`` for serialisation only. Parsing untrusted
XML would be a security concern; writing it is not.
"""

from __future__ import annotations

from xml.etree.ElementTree import Element, SubElement, tostring

from parity.domain.models import CaseOutcome, RunReport, Verdict

SUITE_NAME = "parity"

#: XML 1.0 forbids most control characters outright. Model output can contain
#: them, and an unescapable byte would produce a file no CI system can read.
_ILLEGAL = {c: None for c in range(0x20) if c not in (0x09, 0x0A, 0x0D)}


def _clean(text: str) -> str:
    return text.translate(_ILLEGAL)


def _case_element(outcome: CaseOutcome) -> Element:
    element = Element(
        "testcase",
        {
            "name": outcome.case_id,
            "classname": f"{SUITE_NAME}.{'.'.join(outcome.tags) if outcome.tags else 'case'}",
            "time": f"{outcome.duration_ms / 1000:.3f}",
        },
    )
    reason = _clean(outcome.primary_reason or "outputs differ; no judge configured")

    if outcome.verdict is Verdict.ERROR:
        child = SubElement(element, "error", {"type": "ReplayError", "message": reason[:500]})
        child.text = _clean(outcome.error or reason)
    elif outcome.verdict is Verdict.BROKEN:
        failed = [c for c in outcome.checks if c.blocking_failure] or list(outcome.failed_checks)
        child = SubElement(
            element,
            "failure",
            {
                "type": failed[0].check if failed else "SemanticRegression",
                "message": reason[:500],
            },
        )
        child.text = _clean(
            "\n".join(f"[{c.check}] {c.message}" for c in outcome.failed_checks) or reason
        )
    elif outcome.verdict is Verdict.UNVERIFIED:
        SubElement(element, "skipped", {"message": reason[:500]})

    return element


def render_junit(report: RunReport) -> str:
    """Serialise the report as a JUnit XML document."""
    summary = report.summary
    total_seconds = sum(o.duration_ms for o in report.outcomes) / 1000

    suites = Element(
        "testsuites",
        {
            "name": SUITE_NAME,
            "tests": str(summary.total),
            "failures": str(summary.broken),
            "errors": str(summary.error),
            "skipped": str(summary.unverified),
            "time": f"{total_seconds:.3f}",
        },
    )
    suite = SubElement(
        suites,
        "testsuite",
        {
            "name": f"{report.baseline_ref} → {report.candidate_ref}",
            "tests": str(summary.total),
            "failures": str(summary.broken),
            "errors": str(summary.error),
            "skipped": str(summary.unverified),
            "time": f"{total_seconds:.3f}",
            "timestamp": report.created_at.isoformat(),
        },
    )
    properties = SubElement(suite, "properties")
    for key, value in (
        ("parity.version", report.parity_version),
        ("parity.run_id", report.run_id),
        ("parity.judge", report.judge),
        ("parity.baseline", str(report.baseline_ref)),
        ("parity.candidate", str(report.candidate_ref)),
        ("parity.baseline_source", report.baseline_source),
    ):
        SubElement(properties, "property", {"name": key, "value": _clean(value)})

    for outcome in report.outcomes:
        suite.append(_case_element(outcome))

    body = tostring(suites, encoding="unicode")
    return f'<?xml version="1.0" encoding="UTF-8"?>\n{body}\n'
