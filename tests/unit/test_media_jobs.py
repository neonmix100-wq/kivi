from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from kivi.media.jobs import (
    JobStatus,
    MediaJobError,
    MediaJobNotFound,
    MediaJobQueue,
)


def _write_artifact(path: Path) -> Path:
    path.write_bytes(b"png")
    return path


def test_submit_runner_and_artifact(tmp_path: Path) -> None:
    queue = MediaJobQueue(tmp_path / "jobs")
    try:
        job = queue.submit(
            "image",
            {"prompt": "kivi"},
            lambda params, directory: _write_artifact(directory / "image.png"),
        )
        done = queue.wait(job.id, timeout_s=5)
    finally:
        queue.close()
    assert done.status is JobStatus.DONE
    assert done.artifact.endswith("image.png")
    assert (tmp_path / "jobs" / job.id / "job.json").is_file()
    assert done.params == {"prompt": "kivi"}


def test_runner_failure_is_persisted(tmp_path: Path) -> None:
    queue = MediaJobQueue(tmp_path / "jobs")
    try:
        job = queue.submit("image", {}, lambda params, directory: 1 / 0)
        done = queue.wait(job.id, timeout_s=5)
    finally:
        queue.close()
    assert done.status is JobStatus.FAILED
    assert "ZeroDivisionError" in done.error


def test_cancel_queued_job(tmp_path: Path) -> None:
    queue = MediaJobQueue(tmp_path / "jobs")
    gate = __import__("threading").Event()
    try:
        first = queue.submit("image", {}, lambda params, directory: gate.wait(5))
        second = queue.submit("image", {}, lambda params, directory: None)
        cancelled = queue.cancel(second.id)
        gate.set()
        queue.wait(first.id, timeout_s=5)
    finally:
        queue.close()
    assert cancelled.status is JobStatus.CANCELLED
    assert second.id in {job.id for job in queue.list()}


def test_restart_marks_unfinished_jobs_interrupted(tmp_path: Path) -> None:
    root = tmp_path / "jobs"
    queue = MediaJobQueue(root)
    gate = __import__("threading").Event()
    try:
        job = queue.submit("image", {}, lambda params, directory: gate.wait(5))
        time.sleep(0.05)
        queue.close()
    finally:
        gate.set()
    restarted = MediaJobQueue(root)
    try:
        loaded = restarted.get(job.id)
    finally:
        restarted.close()
    assert loaded.status is JobStatus.INTERRUPTED
    assert loaded.error


def test_unknown_and_invalid_id(tmp_path: Path) -> None:
    queue = MediaJobQueue(tmp_path / "jobs")
    try:
        with pytest.raises(MediaJobNotFound):
            queue.get("media-000000000000")
        with pytest.raises(MediaJobError):
            queue.get("../escape")
    finally:
        queue.close()


def test_params_limit_and_kind(tmp_path: Path) -> None:
    queue = MediaJobQueue(tmp_path / "jobs")
    try:
        with pytest.raises(MediaJobError, match="kind"):
            queue.submit("", {}, lambda params, directory: None)
        with pytest.raises(MediaJobError, match="too large"):
            queue.submit("image", {"prompt": "x" * 40_000}, lambda params, directory: None)
    finally:
        queue.close()


def test_artifact_metadata_mapping(tmp_path: Path) -> None:
    queue = MediaJobQueue(tmp_path / "jobs")
    try:
        job = queue.submit(
            "image",
            {},
            lambda params, directory: {"artifact": str(directory / "x.jpg")},
        )
        result = queue.wait(job.id, timeout_s=5)
    finally:
        queue.close()
    assert result.artifact.endswith("x.jpg")
    assert json.loads((tmp_path / "jobs" / job.id / "job.json").read_text())["status"] == "done"
