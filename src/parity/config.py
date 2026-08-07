"""Configuration loading.

One TOML file, ``parity.toml``, discovered by walking up from the working
directory the way ``git`` finds its root. Parsed with the standard library's
``tomllib`` — no YAML, which keeps an entire class of deserialisation problems
out of the project.

Credentials never appear in config. Config names the *environment variable* that
holds a key, so a ``parity.toml`` is safe to commit.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from parity.adapters.providers.registry import ProviderConfig
from parity.domain.policy import CheckSettings, GatePolicy
from parity.errors import ConfigError
from parity.security.limits import Limits
from parity.security.redaction import Category

CONFIG_FILENAME = "parity.toml"
DEFAULT_WORKDIR = ".parity"


class BaselineConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    store: str = "jsonl"
    path: str = f"{DEFAULT_WORKDIR}/baseline.jsonl"


class RunsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = f"{DEFAULT_WORKDIR}/runs"


class SecurityConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    redact: bool = True
    """Redaction is on by default and turning it off is a deliberate act."""

    categories: tuple[Category, ...] = ("credential", "pii")
    limits: Limits = Field(default_factory=Limits)


class JudgeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    provider: str = "ollama"
    """Key into the ``providers`` table."""

    model: str = "llama3.1"
    min_confidence: float = Field(default=0.34, ge=0.0, le=1.0)
    max_tokens: int = Field(default=400, gt=0)


class ReplayConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    concurrency: int = Field(default=4, ge=1, le=64)
    max_retries: int = Field(default=2, ge=0, le=10)
    retry_base_delay_seconds: float = Field(default=1.0, ge=0.0)
    retry_max_delay_seconds: float = Field(default=30.0, ge=0.0)


def _default_providers() -> dict[str, ProviderConfig]:
    """Providers available without any configuration file.

    ``fake`` so the tool is usable immediately with no credentials, and
    ``ollama`` because it is the free path to real models.
    """
    return {
        "fake": ProviderConfig(kind="fake", name="fake"),
        "ollama": ProviderConfig(kind="ollama", name="ollama"),
        "openai": ProviderConfig(kind="openai", name="openai"),
        "anthropic": ProviderConfig(kind="anthropic", name="anthropic"),
    }


class ParityConfig(BaseModel):
    """Complete configuration for a project."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    root: Path = Field(default_factory=Path.cwd)
    """Directory that relative paths resolve against. Never read from the file."""

    baseline: BaselineConfig = Field(default_factory=BaselineConfig)
    runs: RunsConfig = Field(default_factory=RunsConfig)
    checks: CheckSettings = Field(default_factory=CheckSettings)
    gate: GatePolicy = Field(default_factory=GatePolicy)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    judge: JudgeConfig = Field(default_factory=JudgeConfig)
    replay: ReplayConfig = Field(default_factory=ReplayConfig)
    providers: dict[str, ProviderConfig] = Field(default_factory=_default_providers)

    def baseline_path(self) -> Path:
        return self._resolve(self.baseline.path)

    def runs_path(self) -> Path:
        return self._resolve(self.runs.path)

    def _resolve(self, value: str) -> Path:
        candidate = Path(value)
        return candidate if candidate.is_absolute() else (self.root / candidate)

    def provider(self, name: str) -> ProviderConfig:
        try:
            return self.providers[name]
        except KeyError:
            known = ", ".join(sorted(self.providers)) or "(none)"
            raise ConfigError(
                f"no provider named {name!r} in configuration. Known providers: {known}"
            ) from None

    def snapshot(self) -> dict[str, Any]:
        """Config as recorded in a run report.

        ``providers`` is reduced to names: base URLs can carry internal
        hostnames, and a run report is meant to be shareable.
        """
        payload = self.model_dump(mode="json", exclude={"root", "providers"})
        payload["providers"] = sorted(self.providers)
        return payload


def find_config_file(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` looking for ``parity.toml``."""
    current = (start or Path.cwd()).resolve()
    for directory in (current, *current.parents):
        candidate = directory / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
    return None


def load_config(path: Path | None = None, *, start: Path | None = None) -> ParityConfig:
    """Load configuration, falling back to defaults when no file exists.

    Defaults are usable on their own: with no ``parity.toml`` at all, the fake
    and Ollama providers work and nothing requires a credential.
    """
    config_path = path or find_config_file(start)
    if config_path is None:
        return ParityConfig(root=(start or Path.cwd()).resolve())

    if not config_path.is_file():
        raise ConfigError(f"configuration file not found: {config_path}")

    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{config_path}: could not be read: {exc}") from exc

    if "root" in raw:
        raise ConfigError(
            f"{config_path}: 'root' is derived from the config file location "
            "and cannot be set in the file"
        )

    providers = _default_providers()
    for name, spec in (raw.pop("providers", None) or {}).items():
        if not isinstance(spec, dict):
            raise ConfigError(f"{config_path}: providers.{name} must be a table")
        providers[name] = _parse_section(
            ProviderConfig, {"name": name, **spec}, where=f"{config_path}: providers.{name}"
        )

    try:
        return ParityConfig(root=config_path.parent.resolve(), providers=providers, **raw)
    except ValidationError as exc:
        raise ConfigError(f"{config_path}: {_format_validation_error(exc)}") from exc


def _parse_section(model: type[BaseModel], data: dict[str, Any], *, where: str) -> Any:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"{where}: {_format_validation_error(exc)}") from exc


def _format_validation_error(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"{location}: {error['msg']}")
    return "; ".join(lines)


DEFAULT_CONFIG_TEMPLATE = """\
# Parity configuration.
# Credentials are never stored here — only the names of environment variables.

[baseline]
store = "jsonl"           # "jsonl" or "sqlite"
path = ".parity/baseline.jsonl"

[runs]
path = ".parity/runs"

[checks]
# Inference is what makes the gate work without authoring tests.
infer_required_fields = true
infer_tool_calls = true
max_length_delta_ratio = 0.5
length_severity = "warning"
# numeric_tolerance = 0.01      # uncomment to compare numbers in JSON output
# disabled = ["length_delta"]

[gate]
max_failures = 0
fail_on_unverified = false      # set true once a judge is configured
min_cases = 1

[security]
redact = true
categories = ["credential", "pii"]

[judge]
enabled = false
provider = "ollama"             # runs locally, costs nothing
model = "llama3.1"

[replay]
concurrency = 4
max_retries = 2

[providers.ollama]
kind = "ollama"
base_url = "http://localhost:11434"

[providers.openai]
kind = "openai"
api_key_env = "OPENAI_API_KEY"

[providers.anthropic]
kind = "anthropic"
api_key_env = "ANTHROPIC_API_KEY"

# Any OpenAI-compatible server — vLLM, LM Studio, llama.cpp, a gateway.
# [providers.local]
# kind = "openai"
# base_url = "http://localhost:8000/v1"
# require_key = false
"""
