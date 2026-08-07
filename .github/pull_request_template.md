## What and why

<!-- What changes, and what problem it solves. Link an issue if there is one. -->

## Checklist

- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `mypy` passes
- [ ] `pytest` passes, coverage still above the gate
- [ ] No network call added to the test suite
- [ ] `CHANGELOG.md` updated under `Unreleased`, if a user would notice

<!-- If you added a check: -->
- [ ] Tested that it fires when it should **and** stays quiet when it should not
- [ ] Severity chosen deliberately (`BLOCKING` only when reasonable people would not disagree)
- [ ] Listed in `parity checks` and in the README table

<!-- If you added a provider: -->
- [ ] `ProviderError.retryable` set correctly for each failure mode
- [ ] Tested with `respx`: success, retryable status, permanent status, malformed body
- [ ] No credential can reach a log, a store, or an error message
