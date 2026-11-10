from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from kivi.elevated_helper import Execution, _minimal_env, execute_request, run_fixed_command
from kivi.kernel.db import Database
from kivi.safety.elevated import (
    ALLOWED_SERVICES,
    MAX_STREAM_CHARS,
    ElevatedBroker,
    ElevatedError,
    InvalidParams,
    KillSwitchLike,
    LaunchError,
    UnknownOperation,
    read_result,
    resolve_operation,
    result_file_path,
)

REQUEST_TIME = "2026-11-11T00:00:00+00:00"


@pytest.fixture()
def db(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "kivi.db")
    try:
        yield database
    finally:
        database.close()


class FakeLauncher:
    def __init__(self, code: int = 42) -> None:
        self.code = code
        self.calls: list[tuple[Path, Path]] = []

    def __call__(self, helper: Path, request: Path) -> int:
        self.calls.append((Path(helper), Path(request)))
        return self.code


class FakeRunner:
    def __init__(self, execution: Execution | None = None) -> None:
        self.execution = execution or Execution(returncode=0, stdout="ok\n")
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv: Sequence[str], timeout_s: float) -> Execution:
        self.calls.append((list(argv), timeout_s))
        return self.execution


class FakeSwitch:
    def __init__(self, engaged: bool) -> None:
        self.engaged = engaged

    def is_engaged(self) -> bool:
        return self.engaged


def make_broker(
    tmp_path: Path,
    db: Database,
    *,
    launcher: FakeLauncher | None = None,
    killswitch: KillSwitchLike | Callable[[], bool] | None = None,
    helper: Path | None = None,
) -> tuple[ElevatedBroker, FakeLauncher]:
    fake = launcher or FakeLauncher()
    broker = ElevatedBroker(
        db,
        request_dir=tmp_path / "elevated",
        launcher=fake,
        killswitch=killswitch,
        helper=helper,
    )
    return broker, fake


def make_request(
    directory: Path,
    *,
    request_id: str = "abc123",
    operation: str = "dns.flush",
    params: dict[str, object] | None = None,
    extra: dict[str, object] | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "version": 1,
        "request_id": request_id,
        "operation": operation,
        "params": {} if params is None else params,
        "created_at": REQUEST_TIME,
    }
    if extra:
        payload.update(extra)
    path = directory / f"request-{request_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# -- allowlist ------------------------------------------------------------
def test_allowlist_builds_fixed_argv() -> None:
    dns = resolve_operation("dns.flush")
    assert dns.argv == ("ipconfig", "/flushdns")
    assert dns.params == {}

    query = resolve_operation("service.query", {"service": "KiviAgent"})
    assert query.argv == ("sc.exe", "query", "KiviAgent")

    restart = resolve_operation("service.restart", {"service": "KiviAgent"})
    assert restart.argv == ("sc.exe", "restart", "KiviAgent")
    assert restart.spec.timeout_s > 0
    assert "KiviAgent" in ALLOWED_SERVICES


def test_unknown_operation_is_rejected() -> None:
    with pytest.raises(UnknownOperation):
        resolve_operation("shell.exec")
    with pytest.raises(UnknownOperation):
        resolve_operation("dns.flush; whoami")


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"service": "Spooler"},
        {"service": "kiviagent"},
        {"service": 1},
        {"service": "KiviAgent", "extra": "x"},
        {"service": ["KiviAgent"]},
    ],
)
def test_service_params_are_strict(params: dict[str, object]) -> None:
    with pytest.raises(InvalidParams):
        resolve_operation("service.restart", params)


# -- broker decisions -----------------------------------------------------
def test_broker_requires_approval(tmp_path: Path, db: Database) -> None:
    broker, launcher = make_broker(tmp_path, db)

    result = broker.request("dns.flush")

    assert result.ok is False
    assert result.status == "denied"
    assert result.decision == "deny"
    assert launcher.calls == []
    audit = db.recent_audit()[0]
    assert audit["actor"] == "elevated"
    assert audit["action"] == "elevated.dns.flush"
    assert audit["decision"] == "deny"
    assert "approval" in audit["reason"]
    assert not (tmp_path / "elevated").exists()


def test_broker_denies_unknown_operation(tmp_path: Path, db: Database) -> None:
    broker, launcher = make_broker(tmp_path, db)

    result = broker.request("shell.exec", {"argv": ["cmd"]}, approved=True)

    assert result.ok is False
    assert result.status == "denied"
    assert "unknown operation" in result.message
    assert launcher.calls == []
    audit = db.recent_audit()[0]
    assert audit["action"] == "elevated.shell.exec"
    assert audit["decision"] == "deny"
    assert audit["reason"] == "unknown operation"


def test_broker_denies_invalid_params(tmp_path: Path, db: Database) -> None:
    broker, launcher = make_broker(tmp_path, db)

    result = broker.request("service.restart", {"service": "Spooler"}, approved=True)

    assert result.ok is False
    assert result.status == "denied"
    assert "not allowlisted" in result.message
    assert launcher.calls == []
    audit = db.recent_audit()[0]
    assert audit["decision"] == "deny"
    assert audit["reason"] == "invalid parameters"


def test_approved_request_is_written_and_launched(tmp_path: Path, db: Database) -> None:
    broker, launcher = make_broker(tmp_path, db)

    result = broker.request("service.query", {"service": "KiviAgent"}, approved=True)

    assert result.ok is True
    assert result.status == "started"
    assert result.decision == "allow"
    assert result.launcher_code == 42
    assert result.request_path is not None
    assert result.result_path == result.request_path.with_suffix(".result.json")
    assert result.request_path.parent == tmp_path / "elevated"
    assert result.as_dict()["request_path"] == str(result.request_path)

    payload = json.loads(result.request_path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["request_id"] == result.request_id
    assert payload["operation"] == "service.query"
    assert payload["params"] == {"service": "KiviAgent"}
    assert "argv" not in payload

    assert len(launcher.calls) == 1
    helper, request = launcher.calls[0]
    assert helper.name == "elevated_helper.py"
    assert helper.is_file()
    assert request == result.request_path

    audit = db.recent_audit()[0]
    assert audit["action"] == "elevated.service.query"
    assert audit["decision"] == "allow"
    assert audit["target"] == str(result.request_path)
    details = json.loads(audit["details"])
    assert details["request_id"] == result.request_id
    assert details["argv"] == ["sc.exe", "query", "KiviAgent"]


def test_killswitch_blocks_requests(tmp_path: Path, db: Database) -> None:
    broker, launcher = make_broker(tmp_path, db, killswitch=lambda: True)

    result = broker.request("dns.flush", approved=True)

    assert result.ok is False
    assert result.status == "denied"
    assert "kill switch" in result.message
    assert launcher.calls == []
    audit = db.recent_audit()[0]
    assert audit["decision"] == "deny"
    assert audit["reason"] == "kill switch engaged"


def test_killswitch_object_blocks_requests(tmp_path: Path, db: Database) -> None:
    broker, launcher = make_broker(tmp_path, db, killswitch=FakeSwitch(True))

    result = broker.request("dns.flush", approved=True)

    assert result.status == "denied"
    assert launcher.calls == []
    assert db.recent_audit()[0]["decision"] == "deny"


def test_broken_killswitch_fails_closed(tmp_path: Path, db: Database) -> None:
    def broken() -> bool:
        raise RuntimeError("stop check failed")

    broker, launcher = make_broker(tmp_path, db, killswitch=broken)

    result = broker.request("dns.flush", approved=True)

    assert result.status == "denied"
    assert launcher.calls == []


def test_launcher_failure_is_audited(tmp_path: Path, db: Database) -> None:
    broker, _ = make_broker(tmp_path, db, launcher=FakeLauncher(code=5))

    result = broker.request("dns.flush", approved=True)

    assert result.ok is False
    assert result.status == "error"
    assert result.decision == "error"
    assert "5" in result.message
    audit = db.recent_audit()[0]
    assert audit["decision"] == "error"
    assert audit["actor"] == "elevated"


def test_launcher_exception_is_audited(tmp_path: Path, db: Database) -> None:
    def broken(helper: Path, request: Path) -> int:
        raise LaunchError("UAC cancelled")

    broker = ElevatedBroker(
        db,
        request_dir=tmp_path / "elevated",
        launcher=broken,
    )

    result = broker.request("dns.flush", approved=True)

    assert result.status == "error"
    assert "UAC cancelled" in result.message
    assert db.recent_audit()[0]["decision"] == "error"


def test_missing_helper_is_an_error(tmp_path: Path, db: Database) -> None:
    broker, launcher = make_broker(tmp_path, db, helper=tmp_path / "missing.py")

    result = broker.request("dns.flush", approved=True)

    assert result.status == "error"
    assert "missing" in result.message
    assert launcher.calls == []
    assert db.recent_audit()[0]["decision"] == "error"


def test_request_write_failure_is_audited(tmp_path: Path, db: Database) -> None:
    blocker = tmp_path / "elevated"
    blocker.write_text("not a directory", encoding="utf-8")
    broker = ElevatedBroker(db, request_dir=blocker, launcher=FakeLauncher())

    result = broker.request("dns.flush", approved=True)

    assert result.status == "error"
    assert "cannot write request file" in result.message
    assert db.recent_audit()[0]["decision"] == "error"


# -- helper ---------------------------------------------------------------
def test_default_runner_has_no_shell_and_no_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        captured["argv"] = argv
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("kivi.elevated_helper.subprocess.run", fake_run)

    execution = run_fixed_command(["ipconfig", "/flushdns"], 30.0)

    assert execution.returncode == 0
    assert captured["argv"] == ["ipconfig", "/flushdns"]
    assert captured["shell"] is False
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["timeout"] == 30.0
    assert captured["check"] is False


def test_helper_executes_fixed_argv_with_fake_runner(tmp_path: Path) -> None:
    request = make_request(
        tmp_path,
        operation="service.query",
        params={"service": "KiviAgent"},
    )
    runner = FakeRunner(Execution(returncode=0, stdout="SERVICE_NAME: KiviAgent\n"))

    result = execute_request(request, runner=runner)

    assert runner.calls == [(["sc.exe", "query", "KiviAgent"], 30.0)]
    assert result["ok"] is True
    assert result["returncode"] == 0
    assert result["stdout"] == "SERVICE_NAME: KiviAgent\n"
    assert result["stderr"] == ""
    assert result["truncated"] is False
    assert result["error"] == ""
    assert result["request_id"] == "abc123"
    assert result["operation"] == "service.query"
    assert set(result) == {
        "version",
        "request_id",
        "operation",
        "ok",
        "returncode",
        "stdout",
        "stderr",
        "truncated",
        "error",
        "finished_at",
    }
    stored = json.loads(result_file_path(request).read_text(encoding="utf-8"))
    assert stored == result
    assert read_result(result_file_path(request)) == result


def test_helper_ignores_client_supplied_argv(tmp_path: Path) -> None:
    request = make_request(
        tmp_path,
        extra={"argv": ["cmd", "/c", "calc"], "executable": "cmd.exe"},
    )
    runner = FakeRunner()

    result = execute_request(request, runner=runner)

    assert result["ok"] is True
    assert runner.calls[0][0] == ["ipconfig", "/flushdns"]


def test_helper_rejects_unknown_operation(tmp_path: Path) -> None:
    request = make_request(tmp_path, operation="shell.exec", params={"argv": ["cmd"]})
    runner = FakeRunner()

    result = execute_request(request, runner=runner)

    assert result["ok"] is False
    assert result["returncode"] is None
    assert "unknown operation" in result["error"]
    assert runner.calls == []
    assert result_file_path(request).is_file()


def test_helper_revalidates_service_params(tmp_path: Path) -> None:
    request = make_request(
        tmp_path,
        operation="service.restart",
        params={"service": "Spooler"},
    )
    runner = FakeRunner()

    result = execute_request(request, runner=runner)

    assert result["ok"] is False
    assert "not allowlisted" in result["error"]
    assert runner.calls == []


def test_helper_reports_nonzero_exit(tmp_path: Path) -> None:
    request = make_request(tmp_path)
    runner = FakeRunner(Execution(returncode=1, stderr="access denied\n"))

    result = execute_request(request, runner=runner)

    assert result["ok"] is False
    assert result["returncode"] == 1
    assert result["error"] == "exit code 1"
    assert result["stderr"] == "access denied\n"


def test_helper_reports_timeout(tmp_path: Path) -> None:
    request = make_request(tmp_path)
    runner = FakeRunner(Execution(returncode=124, timed_out=True))

    result = execute_request(request, runner=runner)

    assert result["ok"] is False
    assert result["error"] == "operation timed out"


def test_helper_truncates_output(tmp_path: Path) -> None:
    request = make_request(tmp_path)
    runner = FakeRunner(
        Execution(
            returncode=0,
            stdout="x" * (MAX_STREAM_CHARS + 5),
            stderr="y" * (MAX_STREAM_CHARS + 5),
        )
    )

    result = execute_request(request, runner=runner)

    assert len(result["stdout"]) == MAX_STREAM_CHARS
    assert len(result["stderr"]) == MAX_STREAM_CHARS
    assert result["truncated"] is True


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        '{"version": 1}',
        '{"version": 99, "request_id": "abc123", "operation": "dns.flush"}',
    ],
)
def test_helper_rejects_malformed_request(tmp_path: Path, text: str) -> None:
    request = tmp_path / "request-bad.json"
    request.write_text(text, encoding="utf-8")
    runner = FakeRunner()

    result = execute_request(request, runner=runner)

    assert result["ok"] is False
    assert result["returncode"] is None
    assert result["error"]
    assert runner.calls == []
    assert result_file_path(request).is_file()
    assert read_result(result_file_path(request)) == result


def test_helper_accepts_utf8_bom(tmp_path: Path) -> None:
    request = make_request(tmp_path)
    request.write_bytes(b"\xef\xbb\xbf" + request.read_bytes())
    runner = FakeRunner()

    result = execute_request(request, runner=runner)

    assert result["ok"] is True
    assert runner.calls[0][0] == ["ipconfig", "/flushdns"]


def test_helper_minimal_env_uses_system_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setenv("WINDIR", r"C:\Windows")
    monkeypatch.setenv("PATH", r"C:\Users\evil")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\evil")

    env = _minimal_env()

    root = r"C:\Windows"
    assert env["PATH"] == os.pathsep.join((os.path.join(root, "System32"), root))
    assert env["NoDefaultCurrentDirectoryInExePath"] == "1"
    assert "USERPROFILE" not in env


def test_read_result_rejects_garbage(tmp_path: Path) -> None:
    path = tmp_path / "garbage.result.json"
    path.write_text("{}", encoding="utf-8")

    with pytest.raises(ElevatedError):
        read_result(path)
