# Architecture

A map for someone changing the code. For *why* the product is shaped this way,
read [design.md](design.md).

## Dependency rule

```
        cli/  ──────────────┐
                            ▼
        app.py  (composition root)
           │                │
           ▼                ▼
     capture/ replay/   adapters/
     report/  gate.py   providers · stores · judges
           │                │
           ▼                ▼
        classify/  ───►  ports/   ◄──── (adapters implement these)
           │
           ▼
        checks/  ───►  domain/
                        models · policy
```

**The rule: `domain`, `checks`, `classify`, and `ports` may not import from
`adapters`.** Everything else follows from it — the offline test suite, the
swappable providers, the ability to embed the library without a CLI.

`app.py` is the only module that knows about both configuration and adapters.

## Module tour

| Module | Responsibility | Notes |
|---|---|---|
| `domain/models.py` | Frozen data: `Case`, `InteractionInput/Output`, `CheckResult`, `Verdict`, `RunReport` | No I/O. Tuples not lists, `extra="forbid"` everywhere. Case identity is a fingerprint of the *input*. |
| `domain/policy.py` | `CheckSettings`, `GatePolicy` | "How much we care", kept separate from "what we measure". |
| `checks/` | Deterministic comparisons | Each returns `SKIP` when inapplicable. Never raises for "does not apply" or "found a problem". |
| `checks/registry.py` | `ALL_CHECKS` in evaluation order | Order is load-bearing: the first blocking failure becomes the reported reason. |
| `classify/classifier.py` | Check results + judge → `Verdict` | The decision order in `classify()` is the product's core logic. |
| `ports/` | `Protocol` interfaces | Structural typing — an adapter needs the right shape, not a base class. |
| `adapters/providers/` | `fake`, `openai` (OpenAI-compatible), `anthropic`, `ollama` | `HttpProviderBase` centralises credentials, timeouts, and retryability. |
| `adapters/stores/` | JSONL and SQLite baselines, file run store | Atomic replace via temp file + rename. |
| `adapters/judges/` | `NoJudge`, `ScriptedJudge`, `LLMJudge` | `None` from `compare()` means *abstain*. |
| `capture/` | Log import and live `Recorder` | Four input shapes recognised; redaction applied before persistence. |
| `replay/runner.py` | Concurrent replay | One case cannot fail the run; order preserved. |
| `gate.py` | `RunReport` + `GatePolicy` → pass/fail | One small function, so the rule that blocks builds stays reviewable. |
| `report/` | terminal · markdown · json · junit | Passing cases are not listed; a wall of green trains people not to read. |
| `security/` | Redaction, resource limits, file permissions | See [SECURITY.md](../SECURITY.md). |
| `config.py` | `parity.toml`, discovered by walking up | `tomllib` only. Config names env vars, never holds a key. |

## Data flow

```
logs ──► capture.import_records ──► Redactor ──► BaselineStore
                                                      │
                                                      ▼
                                              ReplayRunner.run
                                                      │
                                    ┌─────────────────┴──────────────┐
                                    ▼                                ▼
                            LLMProvider.complete            Classifier.classify
                            (candidate model)                       │
                                                     ┌──────────────┴──────────────┐
                                                     ▼                             ▼
                                          deterministic checks            SemanticJudge
                                          (free, offline)                 (optional, last)
                                                     └──────────────┬──────────────┘
                                                                    ▼
                                                                CaseOutcome
                                                                    │
                                                                    ▼
                                                    RunReport ──► RunStore + reporters
                                                                    │
                                                                    ▼
                                                            evaluate_gate ──► exit code
```

## Extension points

Add a **check**: implement the `Check` protocol, register it in `ALL_CHECKS`.
Add a **provider**: implement `LLMProvider` (inherit `HttpProviderBase` for
HTTP), register it in `providers/registry.py`.
Add a **store**: implement `BaselineStore`, register it in `stores/registry.py`.
Add a **judge**: implement `SemanticJudge`; return `None` to abstain.

See [CONTRIBUTING.md](../CONTRIBUTING.md) for the rules each must satisfy.

## Concurrency

The runner uses a `ThreadPoolExecutor`. Providers are I/O-bound, so threads are
the right tool, and keeping adapters synchronous means a contributor can add one
without understanding an async stack.

Results are written into a pre-sized list by index, so output order always
matches baseline order regardless of completion order.

## Testing strategy

- **Offline, always.** `FakeProvider` for models, `respx` for HTTP. No test
  opens a socket or reads a credential.
- **`FakeProvider.Mutation`** reproduces each regression class the tool claims
  to catch, so detection is proven rather than assumed.
- **`FakeClock`** makes retry backoff deterministic and instant.
- **Both stores run the same test suite** via a parametrised fixture, so they
  stay interchangeable.
- **Negative cases matter most** in check tests. A false positive blocks a
  deploy, which is more damaging than a missed finding.
