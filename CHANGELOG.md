# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/parity-dev/parity/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/parity-dev/parity/releases/tag/v0.1.0
