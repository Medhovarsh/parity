# Contributing

## Setup

```bash
python -m venv .venv
. .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
pre-commit install            # optional but recommended
```

## The checks CI runs

```bash
ruff check .
ruff format --check .
mypy
pytest
```

All four must pass. `pytest` enforces 85% coverage.

## Rules that are not negotiable

**No network in the test suite.** Not a slow test, not a skipped test — none. If
a test needs a model, it uses `FakeProvider`. If it needs HTTP, it uses `respx`.
A contributor must be able to run the full suite on a plane with no account
anywhere. CI enforces this by never exposing a secret to the test job.

**No telemetry, ever.** Parity makes no outbound request the user did not ask
for. There is no analytics, no version check, no crash reporting. CI enforces
this: `httpx` may only be imported inside `src/parity/adapters/providers/`, and
no other HTTP client may appear anywhere.

**No `pickle`, `marshal`, `eval`, `exec`, or `yaml.load`.** Baselines are
untrusted input. CI greps for these.

**Redaction runs before persistence.** Any new code path that writes a payload
to a store passes it through `Redactor` first. If you add a rule, add a test that
proves it catches its target *and* a test that proves it leaves something similar
alone — a false positive silently mangles real data.

**The core stays pure.** Nothing in `domain/`, `checks/`, `classify/`, or
`ports/` may import from `adapters/`. That inversion is what makes the suite
offline and the classifier testable. If you find yourself wanting to break it,
add a port instead.

## Adding a check

1. Implement it in `src/parity/checks/`, following the `Check` protocol. Return a
   `SKIP` result when it does not apply — never raise for that.
2. Register it in `ALL_CHECKS` in `checks/registry.py`, in evaluation order.
   Cheap and explanatory checks go first; the classifier reports the first
   blocking failure as the reason.
3. Choose severity deliberately. `BLOCKING` fails a build. If reasonable people
   would disagree about whether your finding is a defect, it is a `WARNING`.
4. Test both directions: it fires when it should, and stays quiet when it should
   not. The second matters more — a false positive blocks a deploy.
5. Add a one-line description to the table in `parity checks` and in the README.

## Adding a provider

1. Implement the `LLMProvider` protocol in
   `src/parity/adapters/providers/`. Inherit `HttpProviderBase` for anything
   over HTTP so credential handling and retry classification stay uniform.
2. Classify errors correctly. `ProviderError.retryable` must be `True` only for
   genuinely transient conditions — getting this wrong causes either flaky runs
   or a stampede against a rate limiter.
3. Register it in `providers/registry.py`.
4. Test with `respx`. Cover a success, a retryable status, a permanent status,
   and a malformed body.
5. Never log, store, or include a credential in an error message.

## Style

Match the surrounding code. Comments explain *why*, not *what* — the code
already says what it does. If a decision looks arbitrary, the comment should say
what the alternative was and why it lost.

## Commits and pull requests

Keep commits focused. Explain the reasoning in the body when the change is not
self-evident. Update `CHANGELOG.md` under `Unreleased` for anything a user would
notice.

## Reporting a vulnerability

Do not open a public issue. See [SECURITY.md](SECURITY.md).
