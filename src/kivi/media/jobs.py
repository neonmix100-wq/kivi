"""Persistent local media job queue.

M7 needs an asynchronous contract: submit -> poll -> download/artifact.  The
queue is deliberately provider-agnostic: callers pass a runner callable and
receive a job id immediately.  Jobs are stored under ``data/media/jobs`` as
small JSON files so the UI/CLI can poll them after a restart.  A running job
cannot be resumed by a new process; on startup such jobs are marked
``interrupted`` rather than pretending they are still running.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

_JOB_ID = re.compile(r"^media-[a-f0-9]{12}$")
_MAX_PARAMS = 32 * 1024


class MediaJobError(RuntimeError):
    """Base error for the media job queue."""


class MediaJobNotFound(MediaJobError):
    """The requested job does not exist."""


class MediaJobCancelled(MediaJobError):
    """A queued job was cancelled before it started."""


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


Runner = Callable[[dict[str, Any], Path], Path | str | Mapping[str, Any] | None]


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


@dataclass(slots=True)
class MediaJob:
    """One queued media task."""

    id: str
    kind: str
    params: dict[str, Any]
    status: JobStatus = JobStatus.QUEUED
    artifact: str = ""
    error: str = ""
    created_at: str = field(default_factory=_now)
    started_at: str = ""
    finished_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "params": self.params,
            "status": self.status.value,
            "artifact": self.artifact,
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class MediaJobQueue:
    """A small persistent, single-process job queue for media generation."""

    def __init__(self, root: Path | str = Path("data/media/jobs"), *, workers: int = 1) -> None:
        if workers < 1:
            raise ValueError("workers must be positive")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._jobs: dict[str, MediaJob] = {}
        self._futures: dict[str, Future[None]] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="kivi-media",
        )
        self._load()

    # -- persistence -----------------------------------------------------
    def _job_dir(self, job_id: str) -> Path:
        if not _JOB_ID.fullmatch(job_id):
            raise MediaJobError("invalid media job id")
        target = (self.root / job_id).resolve()
        try:
            target.relative_to(self.root.resolve())
        except ValueError as exc:
            raise MediaJobError("media job path escapes the queue root") from exc
        return target

    def _write(self, job: MediaJob) -> None:
        directory = self._job_dir(job.id)
        directory.mkdir(parents=True, exist_ok=True)
        _atomic_write(directory / "job.json", job.as_dict())

    def _load(self) -> None:
        for path in sorted(self.root.glob("media-*/job.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                job = MediaJob(
                    id=str(payload["id"]),
                    kind=str(payload["kind"]),
                    params=dict(payload.get("params") or {}),
                    status=JobStatus(str(payload["status"])),
                    artifact=str(payload.get("artifact", "")),
                    error=str(payload.get("error", "")),
                    created_at=str(payload.get("created_at", _now())),
                    started_at=str(payload.get("started_at", "")),
                    finished_at=str(payload.get("finished_at", "")),
                )
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if job.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
                job.status = JobStatus.INTERRUPTED
                job.error = "interrupted by process restart"
                job.finished_at = _now()
                self._write(job)
            self._jobs[job.id] = job

    # -- public API ------------------------------------------------------
    def submit(self, kind: str, params: Mapping[str, Any], runner: Runner) -> MediaJob:
        """Queue one job and return immediately."""
        kind = kind.strip()
        if not kind:
            raise MediaJobError("kind must not be empty")
        cleaned = dict(params)
        if len(json.dumps(cleaned, ensure_ascii=False).encode("utf-8")) > _MAX_PARAMS:
            raise MediaJobError("job params are too large")
        job = MediaJob(id=f"media-{uuid.uuid4().hex[:12]}", kind=kind, params=cleaned)
        with self._lock:
            self._jobs[job.id] = job
            self._write(job)
            future = self._executor.submit(self._run, job.id, runner)
            self._futures[job.id] = future
        return job

    def _run(self, job_id: str, runner: Runner) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.status is not JobStatus.QUEUED:
                return
            job.status = JobStatus.RUNNING
            job.started_at = _now()
            self._write(job)
        directory = self._job_dir(job_id)
        try:
            result = runner(dict(job.params), directory)
            if isinstance(result, Mapping):
                artifact = str(result.get("artifact", ""))
            elif result is None:
                artifact = ""
            else:
                artifact = str(result)
            with self._lock:
                job.status = JobStatus.DONE
                job.artifact = artifact
                job.finished_at = _now()
                self._write(job)
        except Exception as exc:  # noqa: BLE001 - job boundary
            with self._lock:
                job.status = JobStatus.FAILED
                job.error = f"{type(exc).__name__}: {exc}"[:1000]
                job.finished_at = _now()
                self._write(job)

    def get(self, job_id: str) -> MediaJob:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise MediaJobNotFound(f"media job not found: {job_id}")
            return job

    def list(self, *, limit: int = 50) -> list[MediaJob]:
        if limit < 1:
            return []
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
            return jobs[:limit]

    def cancel(self, job_id: str) -> MediaJob:
        """Cancel a queued job; a running job cannot be cancelled safely."""
        with self._lock:
            job = self.get(job_id)
            if job.status is not JobStatus.QUEUED:
                raise MediaJobError(f"job is not queued: {job.status.value}")
            future = self._futures.get(job_id)
            if future is not None:
                future.cancel()
            job.status = JobStatus.CANCELLED
            job.finished_at = _now()
            self._write(job)
            return job

    def wait(self, job_id: str, *, timeout_s: float | None = None) -> MediaJob:
        """Wait for a submitted job and return its final state."""
        with self._lock:
            future = self._futures.get(job_id)
        if future is not None:
            future.result(timeout=timeout_s)
        return self.get(job_id)

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)

    def __enter__(self) -> MediaJobQueue:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = [
    "JobStatus",
    "MediaJob",
    "MediaJobCancelled",
    "MediaJobError",
    "MediaJobNotFound",
    "MediaJobQueue",
    "Runner",
]
