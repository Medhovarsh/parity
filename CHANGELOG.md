# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Releases authenticate with PyPI trusted publishing. The bootstrap token used
  for the first release is removed; no credential is stored in the repository
  or in a GitHub secret.

### Security

- Redaction now strips PyPI, npm, Hugging Face, Stripe, and SendGrid tokens.
  These reach a baseline the same way any credential does — someone pastes a
  deploy script or a CI log into a prompt — and each grants publish or write
  access to a registry. A PyPI token previously passed through in cleartext.

## [0.2.0] - 2026-08-07

### Changed

- Distribution renamed to `parity-ci` on PyPI, because `parity` is taken. The
  import package and the CLI command are both still `parity`.

### Fixed

- `parity accept` is now idempotent. A run report is a static artefact: after
  accepting it, its outcomes still read `unverified`, so accepting the same
  report twice re-promoted the same outputs, bumping `revision` and overwriting
  `previous_reference` with a value that was no longer true. Acceptance now
  skips cases whose stored output already is the candidate.

### Added

- `parity diff <case>` — structure-first diff of one case. Field-level for JSON,
  tool-call level for agents, word-wrapped unified diff for prose. A dropped
  field is one line, not a wall of red.
- `parity accept` — promote candidate outputs into the baseline so an
  intentional change becomes the new expected behaviour. This is what keeps a
  baseline a living specification instead of a snapshot that rots. Cases
  classified as broken are refused unless named explicitly or forced, so a real
  regression cannot be blessed by accident.
- `parity demo` — a complete example in seconds with no config, no credentials,
  and no network. Every regression class appears exactly once, so the output
  doubles as documentation.
- `--format html` — a self-contained report with inline diffs, no external
  assets, and light/dark support. All model output is escaped.
- Case revision tracking: `revision`, `accepted_at`, and `previous_reference`
  record that a reference moved and what it was before.

- Structured logging, off by default. `--verbose` for human-readable output,
  `--log-json` for JSON lines, both to stderr. Records carry identifiers and
  counts only, and every record is passed through the redaction rules so a
  credential cannot reach a log even by accident.
- arm64 coverage in CI (Linux and Windows on arm; macOS runners are already
  arm64), plus a check asserting the built wheel stays `py3-none-any`.
- Release workflow: tag-triggered, verifies the tag matches the packaged
  version and that the changelog documents it, publishes via PyPI trusted
  publishing with no stored token.
- Dependabot, issue templates, and a pull-request checklist.

## [0.1.0] - 2026-08-07

Initial release. Model-migration safety wedge.

### Added

- `parity init` — scaffold `parity.toml` in a project.
- `parity capture import` — build a behaviour baseline from existing interaction
  logs, with secret redaction applied before anything is persisted.
- `parity replay` — replay every baseline input against a candidate provider and
  model, concurrently, with retry and per-case error isolation.
- `parity gate` — replay plus classification, exiting non-zero on regression, for
  use as a CI step.
- `parity report` — render a stored run as terminal output, JSON, Markdown, or
  JUnit XML.
- `parity baseline` — list, show, and re-redact stored baselines.
- `parity doctor` — check configuration, reachable providers, and store health.
- Deterministic check pipeline: JSON parseability, JSON Schema conformance,
  required fields, tool-call name and argument agreement, format regex, output
  length delta, refusal detection, exact match, and numeric tolerance.
- Optional semantic judge, disabled by default. Ships with a no-op judge and an
  LLM judge that runs against any configured provider, including local Ollama.
- Ports and adapters architecture: providers, stores, judges, and clocks are all
  swappable behind `Protocol` interfaces.
- Providers: fake (offline, deterministic), OpenAI-compatible, Anthropic, Ollama.
- Stores: JSONL and SQLite.
- Security layer: redaction, resource limits, restrictive file permissions.

[Unreleased]: https://github.com/Medhovarsh/parity/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Medhovarsh/parity/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Medhovarsh/parity/releases/tag/v0.1.0
