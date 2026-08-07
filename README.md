# Parity

[![CI](https://github.com/Medhovarsh/parity/actions/workflows/ci.yml/badge.svg)](https://github.com/Medhovarsh/parity/actions/workflows/ci.yml)
[![Security](https://github.com/Medhovarsh/parity/actions/workflows/security.yml/badge.svg)](https://github.com/Medhovarsh/parity/actions/workflows/security.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue)](https://www.python.org)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue)](LICENSE)
[![Checked with mypy](https://img.shields.io/badge/mypy-strict-blue)](https://mypy-lang.org/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)

**A behavioural regression gate for non-deterministic LLM systems.**

Your provider deprecates a model. You have prompts in production. You have to
switch by Friday, and nobody can tell you what will break.

Today that review is someone eyeballing twenty outputs in a notebook and
shipping on vibes. Traditional CI cannot help — it assumes the same input
produces the same output, and yours does not.

Parity captures what your model *actually did* on real inputs, replays those
inputs against the candidate, and classifies every difference:

```
parity replay --candidate openai:gpt-5-mini

╭─ parity replay ──────────────────────────────────────────────╮
│ openai:gpt-4o-mini → openai:gpt-5-mini                       │
│ 847 case(s) · judge: none · run 20260807T142211Z-3f9c1a08    │
╰──────────────────────────────────────────────────────────────╯
 verdict      cases  meaning
 equivalent     794  identical after normalisation
 acceptable       0  differs, judged still correct
 unverified      41  differs, nothing judged it
 broken          12  structurally or semantically regressed
 error            0  could not be replayed

 case          verdict  check             what happened
 a3f1c9e21b8d  broken   required_fields   candidate dropped 1 field(s): invoice.tax_total
 7c02dd41a5e0  broken   tool_calls        candidate did not call expected tool(s): search_orders
 e91b7740aa33  broken   refusal           candidate refused a request the baseline completed
 …
```

A week of dread becomes an afternoon reviewing twelve things.

---

## Why this is not another eval tool

Eval tools ask you to author evals first. That is homework with no deadline, so
it never gets done, so the tooling never gets adopted.

**Parity asks you to author nothing.** It harvests cases from logs you already
have, and it infers what to enforce from what the baseline actually produced:

- the baseline returned JSON → the candidate must still return JSON
- the baseline included `invoice.tax_total` → the candidate must still include it
- the baseline called `search_orders` → the candidate must still call it
- the baseline answered → the candidate must not refuse
- the baseline completed → the candidate must not truncate

None of that needs a language model to detect, so most of a run costs nothing
and finishes fast. A semantic judge is consulted only for what survives, and it
is entirely optional.

## Install

```bash
pip install parity
```

Python 3.11+. Linux, macOS, and Windows, on **x86_64 and arm64**.

The wheel is `py3-none-any` — pure Python, no compiled artefacts, nothing
architecture-specific. CI proves it on both architectures rather than
assuming it, including Linux and Windows on arm.

## Quickstart

```bash
parity init                                  # writes parity.toml
parity capture logs/interactions.jsonl       # build the baseline from your logs
parity replay --candidate openai:gpt-5-mini  # see what changes
```

In CI:

```bash
parity gate --candidate openai:gpt-5-mini --format junit --out parity.xml
```

`gate` exits non-zero when the run breaches your policy, so it drops into a
pipeline as one step. The JUnit output lands in whatever your CI already uses to
display tests.

### Zero cost, no account

Everything except replaying against a hosted API runs locally and free. For real
models with no bill, use [Ollama](https://ollama.com):

```bash
ollama pull llama3.1
parity replay --candidate ollama:llama3.1
```

The `fake` provider needs nothing at all and is what the test suite uses.

## How capture works

Point Parity at logs you already have. Four shapes are recognised:

| Shape | Looks like | Comes from |
|---|---|---|
| Proxy log | `{"request": {...}, "response": {...}}` | gateways, proxies, most LLM observability exports |
| Explicit | `{"input": {...}, "output": {...}}` | Parity's own vocabulary |
| Flat | `{"messages": [...], "completion": "..."}` | hand-written |
| Native | a previously exported case | `parity baseline show` |

Or capture live by wrapping your provider:

```python
from parity.adapters.providers import OpenAICompatibleProvider
from parity.adapters.stores import JsonlBaselineStore
from parity.capture import Recorder
from parity.security import default_redactor

with Recorder(
    OpenAICompatibleProvider(),
    store=JsonlBaselineStore(".parity/baseline.jsonl"),
    redactor=default_redactor(),
) as provider:
    output = provider.complete("gpt-4o-mini", request)
```

Every completion passes through and is also captured, redacted first.

## Verdicts

| Verdict | Meaning |
|---|---|
| `equivalent` | Identical after normalisation. |
| `acceptable` | Differs, and a judge confirmed it still serves the request. |
| `unverified` | Differs, and nothing semantically compared the outputs. |
| `broken` | A blocking check failed, or a judge found a real regression. |
| `error` | The case could not be replayed at all. |

`unverified` is deliberate. When no judge is configured, saying *"this changed
and nobody looked"* is the truthful answer — better than defaulting it to pass,
and better than blocking a deploy on a difference that may be cosmetic. Enable a
judge and set `fail_on_unverified = true` once you trust it.

## Checks

Run `parity checks` for the current list.

| Check | Catches |
|---|---|
| `empty_output` | candidate returned nothing where the baseline produced content |
| `truncation` | candidate was cut off mid-generation |
| `refusal` | candidate declined a request the baseline completed |
| `json_parse` | baseline was JSON and the candidate no longer parses |
| `json_schema` | candidate violates an explicitly declared JSON Schema |
| `required_fields` | candidate dropped a field the baseline produced |
| `tool_calls` | candidate stopped calling, or started calling, a tool |
| `exact_match` | byte-for-byte agreement, when a case demands it |
| `format_regex` | candidate does not match a declared format |
| `numeric_tolerance` | numbers moved beyond the configured tolerance |
| `length_delta` | output length moved further than tolerated |

## The semantic judge

Optional, off by default, and it **abstains rather than guesses**. Anything but
a well-formed, confident verdict becomes `unverified`. A judge that manufactures
confidence is worse than no judge.

```toml
[judge]
enabled = true
provider = "ollama"    # local, free, payloads never leave the machine
model = "llama3.1"
```

Any configured provider works as a judge, including one pointed at your own
gateway.

## Diagnostics

Logging is off by default — the report is the output that matters.

```bash
parity replay --candidate ollama:llama3.1 --verbose      # human-readable, to stderr
parity gate    --candidate ollama:llama3.1 --log-json    # JSON lines, for a collector
```

Logs carry identifiers, counts, durations, and verdicts — never message
content and never model output. Every record additionally passes through the
redaction rules on the way out, so a careless log line added in future leaks
a redaction token instead of a key. Logs go to stderr, so stdout stays clean
for `--format json | jq`.

## Security

Baselines contain real production payloads. Parity treats them accordingly.

- **Redaction runs before anything is persisted**, on by default — API keys,
  JWTs, private keys, bearer tokens, connection-string passwords, emails, card
  numbers (Luhn-checked), SSNs. Replacement is one-way.
- **Resource limits** bound what a hostile or corrupt file can do to the process
  reading it.
- **No `pickle`, no `eval`, no YAML.** Stores are JSONL and SQLite.
- **No telemetry, ever.** The only outbound request is to the provider you named.
- **Credentials live in environment variables**, named by config. A `parity.toml`
  is safe to commit.

See [SECURITY.md](SECURITY.md) for the threat model.

## Configuration

One `parity.toml`, discovered by walking up from the working directory.
`parity init` writes a commented one. Every setting has a working default —
with no config file at all, the `fake` and `ollama` providers work.

## Architecture

Ports and adapters. The core — domain, checks, classification — depends only on
`Protocol` interfaces and never imports an adapter. That inversion is why the
whole test suite runs offline against a fake provider, and why adding a provider
does not require touching the classifier.

```
domain/     frozen models, policy          ← no I/O, no imports outward
checks/     deterministic comparisons
classify/   check results + judge → verdict
ports/      LLMProvider, BaselineStore, RunStore, SemanticJudge, Clock
adapters/   providers · stores · judges     ← the only code that talks to the world
capture/    import and live recording
replay/     concurrent runner
report/     terminal · markdown · json · junit
security/   redaction · limits
app.py      composition root
cli/        typer commands
```

## Exit codes

| Code | Meaning |
|---:|---|
| 0 | Passed. |
| 1 | Gate failed — regressions found. The expected failure. |
| 2 | Configuration error. |
| 3 | Storage error. |
| 4 | Input exceeded a security limit and was refused. |
| 70 | Internal defect. Worth a bug report. |
| 130 | Interrupted. |

Distinct codes let a pipeline tell *"the model regressed"* from *"the pipeline is
misconfigured"* without parsing stderr.

## Development

```bash
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

pytest                  # offline, no credentials, no cost
ruff check . && ruff format --check .
mypy
```

The test suite makes no network calls and requires no API key. If a test needs a
model, it uses the `fake` provider.

## Status

`0.1.0`. The CLI surface and the run-report schema are stable enough to build
on; internals may move before 1.0.

## License

Apache-2.0. See [LICENSE](LICENSE).
