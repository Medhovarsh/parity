"""Run report store: one JSON file per run, in a directory.

Run reports are written once and read whole, and they are the artefact a CI job
uploads. Plain files keep that trivial — no server, no schema migration, and
``cat`` works.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from parity.domain.models import RunReport
from parity.errors import StoreError
from parity.security.limits import (
    DEFAULT_LIMITS,
    Limits,
    ensure_secure_dir,
    guard_file_size,
    harden_permissions,
)

_SUFFIX = ".run.json"


class FileRunStore:
    """Directory of run reports named ``<run_id>.run.json``."""

    def __init__(self, directory: Path | str, *, limits: Limits = DEFAULT_LIMITS) -> None:
        self._dir = Path(directory)
        self._limits = limits
        ensure_secure_dir(self._dir)

    @property
    def location(self) -> str:
        return str(self._dir)

    def _path_for(self, run_id: str) -> Path:
        # run_id is generated internally, but this store may be pointed at a
        # user-supplied id from the CLI. Refuse anything that could escape the
        # directory rather than trusting the caller.
        if not run_id or "/" in run_id or "\\" in run_id or run_id.startswith("."):
            raise StoreError(f"invalid run id {run_id!r}")
        return self._dir / f"{run_id}{_SUFFIX}"

    def save(self, report: RunReport) -> str:
        path = self._path_for(report.run_id)
        handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed explicitly below
            mode="w",
            encoding="utf-8",
            dir=self._dir,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        )
        temp_path = Path(handle.name)
        try:
            with handle:
                handle.write(report.model_dump_json(indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            harden_permissions(temp_path)
            temp_path.replace(path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
        return str(path)

    def load(self, run_id: str) -> RunReport | None:
        path = self._path_for(run_id)
        if not path.exists():
            return None
        guard_file_size(path, self._limits)
        try:
            return RunReport.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise StoreError(f"{path}: malformed run report: {exc}") from exc

    def load_latest(self) -> RunReport | None:
        ids = self.list_run_ids()
        return self.load(ids[0]) if ids else None

    def list_run_ids(self) -> tuple[str, ...]:
        if not self._dir.exists():
            return ()
        paths = sorted(
            self._dir.glob(f"*{_SUFFIX}"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        return tuple(p.name[: -len(_SUFFIX)] for p in paths)
