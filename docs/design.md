# Parity — design

**Status:** accepted, implemented as `0.1.0`
**Date:** 2026-08-07

## The problem

A model provider posts a deprecation notice. A team has prompts in production.
They must switch by a date, and nobody can tell them what will break.

The current process is someone eyeballing twenty outputs in a notebook and
shipping on judgement. Traditional CI cannot help, because CI assumes a given
input produces a given output and an LLM does not. There is no `git diff` for
behaviour.

This is not a niche problem. It recurs on every deprecation, every model upgrade,
every prompt edit, and every tool-schema change — and it is the specific thing
blocking enterprises from shipping AI faster than they currently do.

## Why this shape of product

The market position was chosen before the feature set.

**Growth model: bottom-up open source that becomes a control plane.** The
Databricks / dbt / HashiCorp path — a practitioner installs a free tool without
asking anyone, it becomes the default way work is done, and the employer later
pays for the governance layer above it (audit trail, approvals, attestation,
cross-team policy). This is the only enterprise entry point available to a
small team; the Wise path (attack hidden margin, win on licences and capital) is
closed without capital and regulatory standing.

**Wedge: model-migration safety.** Not "evals", not "observability". A wedge
needs an external deadline, and a provider's deprecation notice supplies one.
The user does not have to be convinced the work matters; a date already decided
that.

**Expansion: the baseline is the moat.** The corpus a team assembles to survive
one migration becomes their behaviour specification permanently. It grows with
traffic, every subsequent change is checked against it, and the switching cost
compounds. Same mechanic as dbt's model graph.

**Budget: regulation converts the artefact into a control.** EU AI Act
enforcement, ISO 42001, and US state law all require evidence that a system
behaves as claimed. A signed record of "who approved which behaviour change" is
that evidence. That is what makes the enterprise tier a line item rather than a
discretionary purchase.

## The central design bet

**Every eval product asks the user to author evals first.** That is homework
with no deadline, so it does not get done, so the tooling never gets adopted.

Parity asks for nothing. It harvests cases from logs that already exist, and it
*infers* what to enforce from what the baseline actually produced:

- baseline returned JSON → the candidate must still return JSON
- baseline included `invoice.tax_total` → the candidate must still include it
- baseline called `search_orders` → the candidate must still call it
- baseline answered → the candidate must not refuse
- baseline completed → the candidate must not truncate

None of that requires a language model to detect. That is the second bet: **most
real migration regressions are structural, and structural checks are free.** A
semantic judge is consulted only for what survives, which keeps a full replay
affordable and makes the tool usable with no account, no key, and no bill.

## The loop

1. **Capture** — read existing interaction logs, or wrap the provider to record
   live. Redact before anything is persisted. Produces a *baseline*: real inputs
   paired with the reference model's real outputs.
2. **Replay** — run those same inputs against the candidate model, concurrently,
   with bounded retries.
3. **Classify** — deterministic checks first; a semantic judge only for what
   they cannot settle.
4. **Report** — terminal, Markdown for a PR comment, JSON for machines, JUnit so
   it lands wherever CI already displays tests.
5. **Gate** — the same run with an exit code, as one CI step.

## Verdicts

| Verdict | Meaning |
|---|---|
| `equivalent` | Identical after normalisation. |
| `acceptable` | Differs; a judge confirmed it still serves the request. |
| `unverified` | Differs; nothing semantically compared the outputs. |
| `broken` | A blocking check failed, or a judge found a real regression. |
| `error` | The case could not be replayed at all. |

`unverified` is the most important decision in the design and the one most
likely to be "simplified" away later.

When no judge is configured, *"this changed and nobody looked"* is the truthful
answer. Defaulting it to pass would make the gate a rubber stamp. Defaulting it
to fail would block deploys on cosmetic rewording and get the tool uninstalled in
a week. Reporting it honestly is what lets a team adopt the gate on day one and
tighten it later by enabling a judge and setting `fail_on_unverified = true`.

The judge follows the same principle: **it abstains rather than guesses.**
Anything but a well-formed, sufficiently confident verdict becomes `unverified`.
A judge that manufactures confidence is worse than no judge, because it converts
"unknown" into false assurance — the exact failure the product exists to prevent.

## Classification order

1. Run the deterministic checks. Free.
2. Any blocking failure ends it → `broken`. There is nothing to ask a judge about
   output that no longer parses.
3. Outputs identical after normalisation → `equivalent`. Also free.
4. Outputs differ, no judge → `unverified`.
5. Otherwise ask the judge. An abstention is also `unverified`.

Steps 1–3 settle the large majority of cases in practice. That is what keeps a
full replay cheap.

## Severity

Checks carry `BLOCKING` or `WARNING`. Blocking failures decide the verdict alone
and short-circuit the judge. A finding is only blocking if reasonable people
would not disagree that it is a defect — a dropped field is blocking, a 60%
length increase is a warning. Teams that want a stricter posture set
`treat_warnings_as_broken`.

## Architecture

Ports and adapters. The core — `domain`, `checks`, `classify` — depends only on
`Protocol` interfaces and never imports an adapter.

```
domain/     frozen models, policy          ← no I/O
checks/     deterministic comparisons
classify/   check results + judge → verdict
ports/      LLMProvider · BaselineStore · RunStore · SemanticJudge · Clock
adapters/   providers · stores · judges     ← the only code that talks to the world
capture/    import and live recording
replay/     concurrent runner
report/     terminal · markdown · json · junit
security/   redaction · limits
app.py      composition root
cli/        typer commands
```

Consequences of that inversion, all of them load-bearing:

- The whole test suite runs offline against a fake provider. No key, no network,
  no cost — for contributors and for CI.
- A provider can be added without touching the classifier.
- The `fake` provider's mutation set (`DROP_FIELD`, `REFUSE`, `TRUNCATE`,
  `BREAK_JSON`, …) lets every regression the tool claims to detect be reproduced
  in a unit test without a real model.

Domain models are frozen and use tuples. A baseline is evidence; mutating it in
place would make a run report unreproducible.

## Runner guarantees

- **One case cannot fail the run.** A provider error, a malformed response, or a
  defect inside a check produces `error` for that case and the run continues. A
  migration review that aborts at case 40 of 800 is useless.
- **Output order matches baseline order.** Concurrency is an implementation
  detail; a report that reshuffles between runs cannot be diffed.
- **Retries are bounded and only for retryable failures.** The provider decides
  what is retryable; backoff is exponential with full jitter, capped.

## Security posture

Baselines contain real production payloads. That is the whole threat model.

- **Redaction before persistence**, on by default, one-way. Turning it off is a
  deliberate act and the CLI warns when it is off.
- **Resource limits** on every untrusted read, failing closed before the
  allocation rather than after running out of memory.
- **No `pickle`, `eval`, `exec`, or YAML.** Stores are JSONL and SQLite.
- **No telemetry, ever.** Enforced in CI: `httpx` may only appear inside the
  provider adapters.
- **Credentials live in environment variables named by config**, so a
  `parity.toml` is safe to commit.

## Deliberate omissions

- **No hosted service, no accounts, no database.** The 0.1 is a CLI and a
  library. The control plane is the commercial layer, and building it before the
  free tool has users would be building the roof first.
- **No inferred JSON Schema.** An inferred schema is a guess, and a guess that
  blocks deploys is worse than no check. Schemas are only enforced when declared.
- **No default numeric tolerance.** The acceptable drift on a confidence score
  and on an invoice total are not the same number, and guessing one is worse
  than asking.
- **No streaming, no async API.** Providers are I/O-bound; a thread pool is the
  right tool and keeps adapters trivial to contribute.
- **No prompt management, no model routing, no cost dashboard.** Adjacent, and
  each would dilute the wedge.

## What would falsify this

- If most real migration regressions turn out to be semantic rather than
  structural, the free deterministic layer stops being sufficient and the cost
  argument collapses.
- If teams cannot easily produce interaction logs, capture is harder than
  assumed and the "author nothing" promise weakens.
- If `unverified` proves to be the majority verdict in practice, the tool feels
  like it is not answering the question, and the judge stops being optional.

Each is measurable from real usage, and each has a response short of abandoning
the design.
