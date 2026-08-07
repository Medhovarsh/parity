# Security Policy

## Reporting a vulnerability

Report suspected vulnerabilities privately through GitHub Security Advisories
(`Security` → `Report a vulnerability`) on this repository. Do not open a public
issue for an unpatched vulnerability.

Expect an acknowledgement within 72 hours and a status update within 7 days.

## Threat model

Parity captures, stores, and replays real model interactions. Those payloads are
the most sensitive thing it touches. The design assumes:

1. **Captured payloads may contain secrets and personal data.** Users paste API
   keys, tokens, customer records, and internal documents into prompts. Parity
   treats every captured payload as untrusted and sensitive.
2. **Baseline files may be shared or committed.** A baseline that leaves the
   machine that produced it must not carry credentials with it.
3. **Baseline files may come from elsewhere.** A baseline handed to you by a
   colleague, or checked out from a branch, is untrusted input to the parser.

## Controls

| Control | Where | Behaviour |
|---|---|---|
| Secret redaction | `parity.security.redaction` | Runs on every payload **before** it is written to a store. On by default. Patterns cover common API-key formats, JWTs, private-key blocks, bearer tokens, emails, credit-card-shaped digits, and connection-string passwords. |
| Redaction is irreversible | `parity.security.redaction` | Redacted values are replaced with a stable token containing a truncated SHA-256 digest — enough to see that two occurrences matched, not enough to recover the value. |
| Resource limits | `parity.security.limits` | Caps on file size, record count, individual payload size, and JSON nesting depth. Applied when reading any store or import file. Rejects rather than exhausts memory. |
| No deserialisation of code | throughout | `pickle`, `marshal`, `eval`, `exec`, and `yaml.load` are not used anywhere in the codebase. Stores are JSONL and SQLite only. |
| Restrictive file permissions | `parity.security.limits` | Baseline and run artefacts are created `0600` on POSIX. On Windows, files inherit the directory ACL; keep `.parity/` out of shared locations. |
| No telemetry | project-wide | Parity makes no network call that the user did not explicitly ask for. There is no analytics, no crash reporting, no version check. The only outbound traffic is to the provider endpoint you name on the command line or in config. |
| No implicit credential read | `parity.adapters.providers` | Credentials are read from the environment variable named in config and are never written to logs, reports, or stores. |
| Offline by default | `parity.checks` | Every deterministic check runs locally. A network call happens only during `replay` against a live provider, or when an LLM judge is explicitly configured. |

## Handling of captured data

- Baselines default to `.parity/` in the project root, which the shipped
  `.gitignore` excludes.
- `parity baseline redact` re-runs redaction over an existing baseline for cases
  captured before a pattern was added.
- Redaction is a mitigation, not a guarantee. Review a baseline before sharing
  it outside the trust boundary that produced it.

## Supply chain

- Runtime dependencies are pinned to compatible ranges and kept deliberately few.
- CI runs `pip-audit` on every push and on a weekly schedule.
- Releases are built from a tagged commit by CI, not from a developer machine.
