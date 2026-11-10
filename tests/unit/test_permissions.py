from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kivi.kernel.db import Database
from kivi.safety.permissions import (
    Grant,
    PermissionEngine,
    PermissionRequest,
)


class FakeEvents:
    def __init__(self) -> None:
        self.events: list[str] = []

    def emit(self, event_type: str, **_: object) -> None:
        self.events.append(event_type)


def make_engine(
    tmp_path: Path,
    *,
    autonomy: str = "A4-Bounded",
    clock: object | None = None,
) -> tuple[PermissionEngine, Database, FakeEvents]:
    db = Database(tmp_path / "kivi.db")
    events = FakeEvents()
    engine = PermissionEngine(
        db,
        autonomy=autonomy,
        workspace=str(tmp_path),
        events=events,
        clock=clock,
    )
    return engine, db, events


def make_request(
    capability: str = "fs.read",
    *,
    risk: str = "R0",
    task_id: str = "t1",
    approved: bool = False,
) -> PermissionRequest:
    return PermissionRequest(
        tool="fs.read",
        capability=capability,
        risk=risk,  # type: ignore[arg-type]
        task_id=task_id,
        approved=approved,
    )


def test_issue_grants_exact_capabilities(tmp_path: Path) -> None:
    engine, db, _ = make_engine(tmp_path)
    grant = engine.issue("t1", ["fs.read"])
    try:
        assert grant.capabilities == {"fs.read"}
        assert engine.current("t1") is grant
    finally:
        db.close()


def test_issue_requires_task_and_capabilities(tmp_path: Path) -> None:
    engine, db, _ = make_engine(tmp_path)
    try:
        with pytest.raises(ValueError, match="task_id"):
            engine.issue("", ["fs.read"])
        with pytest.raises(ValueError, match="capability"):
            engine.issue("t1", [])
        with pytest.raises(ValueError, match="ttl_s"):
            engine.issue("t1", ["fs.read"], ttl_s=0)
    finally:
        db.close()


def test_r0_is_allowed(tmp_path: Path) -> None:
    engine, db, _ = make_engine(tmp_path)
    try:
        grant = engine.issue("t1", ["fs.read"])
        decision = engine.check(make_request(), grant)
    finally:
        db.close()
    assert decision.action == "allow"


def test_missing_capability_is_denied_and_audited(tmp_path: Path) -> None:
    engine, db, _ = make_engine(tmp_path)
    try:
        grant = engine.issue("t1", ["fs.read"])
        decision = engine.check(make_request("fs.write"), grant)
        audit = db.recent_audit()[0]
    finally:
        db.close()
    assert decision.action == "deny"
    assert decision.rule == "grant-capability"
    assert audit["decision"] == "deny"
    assert audit["actor"] == "permission"


def test_no_grant_is_denied(tmp_path: Path) -> None:
    engine, db, _ = make_engine(tmp_path)
    try:
        decision = engine.check(make_request(), None)
    finally:
        db.close()
    assert decision.rule == "grant-missing"


def test_foreign_grant_is_denied(tmp_path: Path) -> None:
    engine, db, _ = make_engine(tmp_path)
    try:
        grant = engine.issue("t1", ["fs.read"])
        decision = engine.check(make_request(task_id="t2"), grant)
    finally:
        db.close()
    assert decision.rule == "grant-task"


def test_expired_grant_is_denied(tmp_path: Path) -> None:
    now = datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    engine, db, _ = make_engine(tmp_path, clock=lambda: now)
    try:
        grant = engine.issue("t1", ["fs.read"], ttl_s=1)
        later = now + timedelta(minutes=1)
        engine._clock = lambda: later  # noqa: SLF001 - test clock
        decision = engine.check(make_request(), grant)
    finally:
        db.close()
    assert decision.rule == "grant-expired"


def test_r1_is_allowed_in_bounded_mode(tmp_path: Path) -> None:
    engine, db, _ = make_engine(tmp_path)
    try:
        grant = engine.issue("t1", ["fs.write"])
        decision = engine.check(make_request("fs.write", risk="R1"), grant)
    finally:
        db.close()
    assert decision.action == "allow"


def test_r2_requires_confirmation(tmp_path: Path) -> None:
    engine, db, _ = make_engine(tmp_path)
    try:
        grant = engine.issue("t1", ["shell.exec"])
        engine.check(make_request("shell.exec", risk="R2"), grant)
        audit = db.recent_audit()[0]
    finally:
        db.close()
    assert audit["decision"] == "confirm"


def test_r2_is_allowed_with_approval(tmp_path: Path) -> None:
    engine, db, _ = make_engine(tmp_path)
    try:
        grant = engine.issue("t1", ["shell.exec"])
        decision = engine.check(
            make_request("shell.exec", risk="R2", approved=True), grant
        )
    finally:
        db.close()
    assert decision.action == "allow"


def test_r4_is_denied_even_with_approval(tmp_path: Path) -> None:
    engine, db, _ = make_engine(tmp_path)
    try:
        grant = engine.issue("t1", ["money.send"])
        decision = engine.check(
            make_request("money.send", risk="R4", approved=True), grant
        )
    finally:
        db.close()
    assert decision.action == "deny"
    assert decision.rule == "risk-deny"


def test_lower_autonomy_is_stricter(tmp_path: Path) -> None:
    engine, db, _ = make_engine(tmp_path, autonomy="A2")
    try:
        grant = engine.issue("t1", ["fs.write"])
        decision = engine.check(make_request("fs.write", risk="R1"), grant)
    finally:
        db.close()
    assert decision.action == "confirm"


def test_a0_denies_everything(tmp_path: Path) -> None:
    engine, db, _ = make_engine(tmp_path, autonomy="A0")
    try:
        grant = engine.issue("t1", ["fs.read"])
        decision = engine.check(make_request(), grant)
    finally:
        db.close()
    assert decision.action == "deny"


def test_unknown_autonomy_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="autonomy"):
        PermissionEngine(workspace=str(tmp_path), autonomy="A9")


def test_revoke_removes_grant(tmp_path: Path) -> None:
    engine, db, _ = make_engine(tmp_path)
    try:
        grant = engine.issue("t1", ["fs.read"])
        assert engine.revoke("t1")
        decision = engine.check(make_request(), grant)
    finally:
        db.close()
    assert decision.rule == "grant-revoked"


def test_denial_emits_event(tmp_path: Path) -> None:
    engine, db, events = make_engine(tmp_path)
    try:
        grant: Grant = engine.issue("t1", ["fs.read"])
        engine.check(make_request("fs.write"), grant)
    finally:
        db.close()
    assert events.events == ["safety.deny"]


def test_decision_require_raises(tmp_path: Path) -> None:
    engine, db, _ = make_engine(tmp_path)
    try:
        grant = engine.issue("t1", ["fs.read"])
        decision = engine.check(make_request("fs.write"), grant)
    finally:
        db.close()
    with pytest.raises(PermissionError):
        decision.require()
