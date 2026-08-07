"""The ``parity`` command-line interface.

Command design follows the workflow the tool exists for:

    parity init                       # once
    parity capture logs.jsonl         # build the baseline from what you have
    parity replay --candidate ...     # see what a model change does
    parity gate --candidate ...       # the same thing, with an exit code, in CI

Every command that can fail does so with a distinct exit code, and every error
message names the file and the fix rather than the stack frame.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from parity import __version__
from parity.app import Application
from parity.checks.registry import ALL_CHECKS
from parity.cli.exit_codes import ExitCode
from parity.config import (
    CONFIG_FILENAME,
    DEFAULT_CONFIG_TEMPLATE,
    ParityConfig,
    find_config_file,
    load_config,
)
from parity.domain.models import Case, ModelRef, RunReport
from parity.errors import (
    ConfigError,
    ParityError,
    ProviderError,
    SecurityLimitExceeded,
    StoreError,
)
from parity.gate import evaluate_gate
from parity.observability import configure_logging
from parity.replay.runner import RunProgress
from parity.report import render_junit, render_markdown
from parity.report.terminal import render_terminal


def _configure_streams() -> None:
    """Force UTF-8 on stdout and stderr.

    Windows consoles still default to a legacy code page (cp1252 in most
    locales), which cannot encode the arrows and ellipses in a report — writing
    one raises ``UnicodeEncodeError`` and takes down an otherwise successful run.
    Reconfiguring is the fix; ``errors="replace"`` means even a terminal that
    cannot render a glyph degrades to a placeholder instead of crashing.

    Called from :func:`main` only, so importing the module never mutates global
    state — which matters for tests and for embedding.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # A redirected or already-detached stream. Not worth failing over.
            continue


app = typer.Typer(
    name="parity",
    help="Behavioural regression gate for non-deterministic LLM systems.",
    no_args_is_help=True,
    add_completion=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
baseline_app = typer.Typer(
    help="Inspect and maintain the behaviour baseline.", no_args_is_help=True
)
app.add_typer(baseline_app, name="baseline")

console = Console()
err_console = Console(stderr=True)

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help=f"Path to {CONFIG_FILENAME}. Defaults to the nearest one."),
]


def _load(ctx: typer.Context) -> ParityConfig:
    """Load configuration once per invocation and cache it on the context."""
    state: dict[str, Any] = ctx.ensure_object(dict)
    cached = state.get("config")
    if isinstance(cached, ParityConfig):
        return cached
    config = load_config(state.get("config_path"))
    state["config"] = config
    return config


def _application(ctx: typer.Context) -> Application:
    return Application(_load(ctx))


def _parse_ref(value: str, *, what: str) -> ModelRef:
    try:
        return ModelRef.parse(value)
    except ValueError as exc:
        raise ConfigError(f"invalid {what}: {exc}") from exc


def _fail(message: str, code: ExitCode) -> typer.Exit:
    err_console.print(f"[bold red]error:[/bold red] {message}")
    return typer.Exit(int(code))


@app.callback()
def root(
    ctx: typer.Context,
    config: ConfigOption = None,
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Suppress progress output.")] = False,
    no_color: Annotated[bool, typer.Option("--no-color", help="Disable ANSI colour.")] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", "-v", help="Log diagnostics to stderr.")
    ] = False,
    log_json: Annotated[bool, typer.Option("--log-json", help="Emit logs as JSON lines.")] = False,
) -> None:
    """Shared options for every subcommand."""
    state: dict[str, Any] = ctx.ensure_object(dict)
    state["config_path"] = config
    state["quiet"] = quiet
    configure_logging(verbose=verbose, json_format=log_json)
    if no_color:
        console.no_color = True
        err_console.no_color = True


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@app.command()
def init(
    directory: Annotated[Path, typer.Argument(help="Where to write the config file.")] = Path(),
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file.")] = False,
) -> None:
    """Write a commented ``parity.toml`` into a project."""
    target = directory / CONFIG_FILENAME
    if target.exists() and not force:
        raise _fail(
            f"{target} already exists. Pass --force to overwrite it.", ExitCode.CONFIG_ERROR
        )
    directory.mkdir(parents=True, exist_ok=True)
    target.write_text(DEFAULT_CONFIG_TEMPLATE, encoding="utf-8")
    console.print(f"[green]wrote[/green] {target}")
    console.print(
        "\nNext: build a baseline from logs you already have —\n"
        "  [bold]parity capture path/to/interactions.jsonl[/bold]"
    )


# ---------------------------------------------------------------------------
# capture
# ---------------------------------------------------------------------------


@app.command()
def capture(
    ctx: typer.Context,
    source: Annotated[Path, typer.Argument(help="JSONL or JSON array of interaction records.")],
    reference: Annotated[
        str | None,
        typer.Option("--reference", "-r", help="Override the recorded model, as provider:model."),
    ] = None,
    tag: Annotated[
        list[str] | None, typer.Option("--tag", "-t", help="Tag every imported case.")
    ] = None,
    strict: Annotated[
        bool, typer.Option("--strict", help="Fail on the first unparseable record.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Parse and report without writing.")
    ] = False,
) -> None:
    """Build or extend the baseline from existing interaction logs."""
    from parity.capture.importer import import_records, iter_jsonl

    application = _application(ctx)
    if not source.is_file():
        raise _fail(f"no such file: {source}", ExitCode.CONFIG_ERROR)

    reference_override = _parse_ref(reference, what="--reference") if reference else None
    result = import_records(
        iter_jsonl(source, limits=application.limits),
        redactor=application.redactor(),
        limits=application.limits,
        reference_override=reference_override,
        tags=tuple(tag or ()),
        strict=strict,
    )

    if result.skipped:
        err_console.print(f"[yellow]skipped {len(result.skipped)} record(s):[/yellow]")
        for message in result.skipped[:10]:
            err_console.print(f"  {message}")
        if len(result.skipped) > 10:
            err_console.print(f"  …and {len(result.skipped) - 10} more")

    if application.redactor() is None:
        err_console.print(
            "[yellow]warning:[/yellow] redaction is disabled; captured payloads are "
            "stored exactly as provided"
        )
    else:
        console.print(f"[dim]{result.redaction.summary()}[/dim]")

    if dry_run:
        console.print(f"[green]parsed[/green] {result.parsed} case(s) (dry run, nothing written)")
        return

    store = application.open_baseline()
    try:
        written = store.extend(result.cases)
        total = store.count()
    finally:
        store.close()

    duplicates = result.parsed - written
    console.print(
        f"[green]captured[/green] {written} new case(s)"
        + (f", {duplicates} already present" if duplicates else "")
        + f" — baseline now holds {total} case(s) at {store.location}"
    )


# ---------------------------------------------------------------------------
# replay / gate
# ---------------------------------------------------------------------------


def _load_cases(
    application: Application,
    *,
    limit: int | None,
    tags: tuple[str, ...],
) -> list[Case]:
    store = application.open_baseline()
    try:
        cases: list[Case] = []
        for case in store.iter_cases():
            if tags and not set(tags) & set(case.tags):
                continue
            cases.append(case)
            if limit is not None and len(cases) >= limit:
                break
        return cases
    finally:
        store.close()


def _execute_replay(
    ctx: typer.Context,
    *,
    candidate: str,
    limit: int | None,
    tags: tuple[str, ...],
    save: bool,
) -> tuple[RunReport, Application]:
    application = _application(ctx)
    candidate_ref = _parse_ref(candidate, what="--candidate")

    cases = _load_cases(application, limit=limit, tags=tags)
    if not cases:
        raise _fail(
            f"baseline at {application.config.baseline_path()} contains no matching cases. "
            "Run `parity capture <logs>` first.",
            ExitCode.STORE_ERROR,
        )

    baseline_ref = cases[0].reference
    quiet = bool(ctx.ensure_object(dict).get("quiet"))

    with application.replay_session(candidate_ref) as runner:
        if quiet:
            outcomes = runner.run(cases)
        else:
            with console.status(f"replaying {len(cases)} case(s) against {candidate_ref}…") as st:

                def on_progress(progress: RunProgress) -> None:
                    st.update(
                        f"replaying {progress.completed}/{progress.total} "
                        f"against {candidate_ref} — last: {progress.verdict.value}"
                    )

                outcomes = runner.run(cases, on_progress=on_progress)

        report = runner.build_report(
            parity_version=__version__,
            baseline_ref=baseline_ref,
            baseline_source=str(application.config.baseline_path()),
            outcomes=outcomes,
            config_snapshot=application.config.snapshot(),
        )

    if save:
        location = application.open_runs().save(report)
        if not quiet:
            # stderr, not stdout: with --format json|junit|markdown and no --out,
            # stdout must contain the document and nothing else, or piping into
            # jq (or any parser) breaks.
            err_console.print(f"[dim]run saved to {location}[/dim]")

    return report, application


CandidateOption = Annotated[
    str, typer.Option("--candidate", "-m", help="Candidate model, as provider:model.")
]
LimitOption = Annotated[
    int | None, typer.Option("--limit", "-n", help="Replay at most this many cases.")
]
TagOption = Annotated[
    list[str] | None, typer.Option("--tag", "-t", help="Only replay cases with this tag.")
]


@app.command()
def replay(
    ctx: typer.Context,
    candidate: CandidateOption,
    limit: LimitOption = None,
    tag: TagOption = None,
    save: Annotated[bool, typer.Option("--save/--no-save", help="Persist the run report.")] = True,
) -> None:
    """Replay the baseline against a candidate model and show what changed."""
    report, _ = _execute_replay(
        ctx, candidate=candidate, limit=limit, tags=tuple(tag or ()), save=save
    )
    render_terminal(report, console=console)


@app.command()
def gate(
    ctx: typer.Context,
    candidate: CandidateOption,
    limit: LimitOption = None,
    tag: TagOption = None,
    save: Annotated[bool, typer.Option("--save/--no-save", help="Persist the run report.")] = True,
    output_format: Annotated[
        str, typer.Option("--format", "-f", help="terminal, markdown, json, or junit.")
    ] = "terminal",
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Write the report to a file.")
    ] = None,
) -> None:
    """Replay, then exit non-zero if the run breaches the gate policy. For CI."""
    report, application = _execute_replay(
        ctx, candidate=candidate, limit=limit, tags=tuple(tag or ()), save=save
    )
    decision = evaluate_gate(report, application.config.gate)
    _emit(report, output_format, out, decision=decision)

    if not decision.passed:
        raise typer.Exit(int(ExitCode.GATE_FAILED))


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _emit(
    report: RunReport,
    output_format: str,
    out: Path | None,
    *,
    decision: Any = None,
) -> None:
    if output_format == "terminal":
        if out is not None:
            file_console = Console(file=out.open("w", encoding="utf-8"), width=100)
            try:
                render_terminal(report, decision, console=file_console)
            finally:
                file_console.file.close()
        else:
            render_terminal(report, decision, console=console)
        return

    if output_format == "markdown":
        text = render_markdown(report, decision)
    elif output_format == "junit":
        text = render_junit(report)
    elif output_format == "json":
        text = report.model_dump_json(indent=2)
    else:
        raise _fail(
            f"unknown format {output_format!r}; expected terminal, markdown, json, or junit",
            ExitCode.CONFIG_ERROR,
        )

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        err_console.print(f"[green]wrote[/green] {out}")
    else:
        # Machine-readable output goes to stdout unformatted so it can be piped.
        sys.stdout.write(text)


@app.command()
def report(
    ctx: typer.Context,
    run_id: Annotated[
        str | None, typer.Argument(help="Run to render. Defaults to the most recent.")
    ] = None,
    output_format: Annotated[
        str, typer.Option("--format", "-f", help="terminal, markdown, json, or junit.")
    ] = "terminal",
    out: Annotated[
        Path | None, typer.Option("--out", "-o", help="Write the report to a file.")
    ] = None,
) -> None:
    """Render a stored run report."""
    application = _application(ctx)
    runs = application.open_runs()
    stored = runs.load(run_id) if run_id else runs.load_latest()
    if stored is None:
        raise _fail(
            f"no run found in {runs.location}" + (f" with id {run_id!r}" if run_id else ""),
            ExitCode.STORE_ERROR,
        )
    decision = evaluate_gate(stored, application.config.gate)
    _emit(stored, output_format, out, decision=decision)


@app.command(name="runs")
def list_runs(ctx: typer.Context) -> None:
    """List stored run ids, newest first."""
    runs = _application(ctx).open_runs()
    ids = runs.list_run_ids()
    if not ids:
        console.print(f"[dim]no runs stored in {runs.location}[/dim]")
        return
    for run_id in ids:
        console.print(run_id)


# ---------------------------------------------------------------------------
# baseline
# ---------------------------------------------------------------------------


@baseline_app.command("stats")
def baseline_stats(ctx: typer.Context) -> None:
    """Summarise what the baseline contains."""
    application = _application(ctx)
    store = application.open_baseline()
    try:
        by_model: dict[str, int] = {}
        by_tag: dict[str, int] = {}
        unredacted = 0
        total = 0
        for case in store.iter_cases():
            total += 1
            by_model[str(case.reference)] = by_model.get(str(case.reference), 0) + 1
            for tag in case.tags:
                by_tag[tag] = by_tag.get(tag, 0) + 1
            if not case.redacted:
                unredacted += 1
    finally:
        store.close()

    console.print(f"[bold]{total}[/bold] case(s) at {store.location}")
    if by_model:
        table = Table(show_header=True, header_style="bold", box=None)
        table.add_column("reference model")
        table.add_column("cases", justify="right")
        for name, count in sorted(by_model.items(), key=lambda kv: -kv[1]):
            table.add_row(name, str(count))
        console.print(table)
    if by_tag:
        console.print("tags: " + ", ".join(f"{k} ({v})" for k, v in sorted(by_tag.items())))
    if unredacted:
        console.print(
            f"[yellow]{unredacted} case(s) were stored without redaction.[/yellow] "
            "Run `parity baseline redact` before sharing this file."
        )


@baseline_app.command("list")
def baseline_list(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many to show.")] = 20,
) -> None:
    """List case ids with a preview of the input."""
    store = _application(ctx).open_baseline()
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("case", style="dim", no_wrap=True)
    table.add_column("reference", no_wrap=True)
    table.add_column("input preview", overflow="ellipsis", max_width=70)
    try:
        for index, case in enumerate(store.iter_cases()):
            if index >= limit:
                break
            preview = next(
                (m.content for m in reversed(case.input.messages) if m.role == "user"),
                case.input.messages[-1].content,
            )
            table.add_row(case.case_id[:12], str(case.reference), preview.replace("\n", " "))
    finally:
        store.close()
    console.print(table)


@baseline_app.command("show")
def baseline_show(
    ctx: typer.Context,
    case_id: Annotated[str, typer.Argument(help="Case id, or a unique prefix.")],
) -> None:
    """Print one case as JSON."""
    store = _application(ctx).open_baseline()
    try:
        found = store.get(case_id)
        if found is None:
            matches = [c for c in store.iter_cases() if c.case_id.startswith(case_id)]
            if len(matches) == 1:
                found = matches[0]
            elif len(matches) > 1:
                raise _fail(
                    f"{case_id!r} matches {len(matches)} cases; use a longer prefix",
                    ExitCode.CONFIG_ERROR,
                )
    finally:
        store.close()
    if found is None:
        raise _fail(f"no case with id {case_id!r}", ExitCode.STORE_ERROR)
    sys.stdout.write(found.model_dump_json(indent=2) + "\n")


@baseline_app.command("redact")
def baseline_redact(
    ctx: typer.Context,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Rewrite without confirming.")] = False,
) -> None:
    """Re-run redaction over every stored case, in place.

    Use after adding a pattern, or on a baseline captured with redaction off.
    Redaction is irreversible — the original values are not recoverable
    afterwards.
    """
    application = _application(ctx)
    redactor = application.redactor()
    if redactor is None:
        raise _fail(
            "redaction is disabled in configuration; enable [security].redact first",
            ExitCode.CONFIG_ERROR,
        )

    store = application.open_baseline()
    try:
        cases = list(store.iter_cases())
        if not cases:
            console.print("[dim]baseline is empty; nothing to do[/dim]")
            return
        if not yes:
            confirmed = typer.confirm(
                f"Rewrite {len(cases)} case(s) at {store.location}? "
                "Redacted values cannot be recovered.",
                default=False,
            )
            if not confirmed:
                console.print("aborted")
                raise typer.Exit(int(ExitCode.OK))

        cleaned = []
        report_total = 0
        for case in cases:
            case, case_report = redactor.case(case)
            report_total += case_report.total
            cleaned.append(case)
        written = store.replace_all(cleaned)
    finally:
        store.close()

    console.print(f"[green]rewrote[/green] {written} case(s); redacted {report_total} value(s)")


# ---------------------------------------------------------------------------
# introspection
# ---------------------------------------------------------------------------


@app.command()
def providers(ctx: typer.Context) -> None:
    """List configured providers."""
    config = _load(ctx)
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("name")
    table.add_column("kind")
    table.add_column("endpoint", overflow="fold")
    table.add_column("key from")
    for name, spec in sorted(config.providers.items()):
        table.add_row(
            name,
            spec.kind,
            spec.base_url or "[dim]default[/dim]",
            spec.api_key_env or "[dim]none needed[/dim]",
        )
    console.print(table)


@app.command()
def checks() -> None:
    """List the deterministic checks and what each one catches."""
    descriptions = {
        "empty_output": "candidate returned nothing where the baseline produced content",
        "truncation": "candidate was cut off mid-generation",
        "refusal": "candidate declined a request the baseline completed",
        "json_parse": "baseline was JSON and the candidate no longer parses",
        "json_schema": "candidate violates an explicitly declared JSON Schema",
        "required_fields": "candidate dropped a field the baseline produced",
        "tool_calls": "candidate stopped calling, or started calling, a tool",
        "exact_match": "byte-for-byte agreement, when a case demands it",
        "format_regex": "candidate does not match a declared format",
        "numeric_tolerance": "numbers moved beyond the configured tolerance",
        "length_delta": "output length moved further than tolerated",
    }
    table = Table(show_header=True, header_style="bold", box=None)
    table.add_column("check")
    table.add_column("catches", overflow="fold")
    for check in ALL_CHECKS:
        table.add_row(check.name, descriptions.get(check.name, ""))
    console.print(table)


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Check configuration, storage, and provider reachability."""
    config = _load(ctx)
    application = Application(config)
    problems = 0

    found = find_config_file()
    console.print(
        f"config      : {found}" if found else "config      : [dim]none found, using defaults[/dim]"
    )
    console.print(f"baseline    : {config.baseline_path()} ({config.baseline.store})")
    console.print(f"runs        : {config.runs_path()}")
    console.print(
        "redaction   : "
        + (
            f"on ({', '.join(config.security.categories)})"
            if config.security.redact
            else "[yellow]OFF[/yellow]"
        )
    )

    try:
        store = application.open_baseline()
        try:
            console.print(f"cases       : {store.count()}")
        finally:
            store.close()
    except (StoreError, SecurityLimitExceeded) as exc:
        problems += 1
        console.print(f"cases       : [red]{exc}[/red]")

    if config.judge.enabled:
        console.print(f"judge       : {config.judge.provider}:{config.judge.model}")
        try:
            application.judge().close()
        except ParityError as exc:
            problems += 1
            console.print(f"              [red]{exc}[/red]")
    else:
        console.print("judge       : [dim]disabled — differing cases report as unverified[/dim]")

    for name in sorted(config.providers):
        spec = config.providers[name]
        if spec.kind == "fake":
            continue
        try:
            application.provider_for(ModelRef(provider=name, model="probe")).close()
            console.print(f"provider {name:<10}: ready")
        except (ConfigError, ProviderError) as exc:
            # A missing key is a normal state for a provider you are not using.
            console.print(f"provider {name:<10}: [yellow]{exc}[/yellow]")

    if problems:
        raise typer.Exit(int(ExitCode.CONFIG_ERROR))
    console.print("\n[green]no blocking problems found[/green]")


@app.command()
def version() -> None:
    """Print the version."""
    console.print(__version__)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Console-script entry point mapping exceptions to exit codes."""
    _configure_streams()
    try:
        app()
    except SystemExit as exc:  # Typer's normal control flow.
        return int(exc.code) if isinstance(exc.code, int) else 0
    except KeyboardInterrupt:
        err_console.print("[yellow]interrupted[/yellow]")
        return int(ExitCode.INTERRUPTED)
    except ConfigError as exc:
        err_console.print(f"[bold red]configuration error:[/bold red] {exc}")
        return int(ExitCode.CONFIG_ERROR)
    except SecurityLimitExceeded as exc:
        err_console.print(f"[bold red]refused:[/bold red] {exc}")
        return int(ExitCode.SECURITY_LIMIT)
    except StoreError as exc:
        err_console.print(f"[bold red]storage error:[/bold red] {exc}")
        return int(ExitCode.STORE_ERROR)
    except ParityError as exc:
        err_console.print(f"[bold red]error:[/bold red] {exc}")
        return int(ExitCode.INTERNAL_ERROR)
    return int(ExitCode.OK)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
