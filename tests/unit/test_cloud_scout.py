from __future__ import annotations

from pathlib import Path

import pytest

from kivi.cloud.broker import HandoffPack, load_handoff
from kivi.cloud.providers import CloudRegistry, load_cloud_registry
from kivi.cloud.scout import (
    CandidateProvider,
    CloudScout,
    ScoutError,
    ScoutQuery,
    ScoutRegistry,
    load_scout_registry,
)
from kivi.kernel.db import Database

CLOUD_YAML = """\
providers:
  - id: existing
    kind: openai
    base_url: https://example.test/v1
    api_key_env: EXISTING_KEY
    default_model: ""
    free_requests_per_day: 10
    free_tokens_per_day: null
    allows_private_data: false
"""


def make_candidate(candidate_id: str, **overrides) -> CandidateProvider:
    values = {
        "id": candidate_id,
        "name": f"Candidate {candidate_id}",
        "capabilities": ("llm",),
        "base_url": "https://example.test/v1",
        "api_key_env": "TEST_CANDIDATE_KEY",
        "default_model": "",
        "free_requests_per_day": 10,
        "data_policy": "public_only",
        "trains_on_data": False,
        "watermark": "none",
    }
    values.update(overrides)
    return CandidateProvider(**values)


def make_scout(
    tmp_path: Path,
    candidates: list[CandidateProvider],
    *,
    providers: CloudRegistry | None = None,
    environ: dict[str, str] | None = None,
) -> tuple[CloudScout, Database]:
    db = Database(tmp_path / "kivi.db")
    scout = CloudScout(
        db,
        providers or CloudRegistry({}),
        ScoutRegistry({item.id: item for item in candidates}),
        environ=environ if environ is not None else {},
    )
    return scout, db


def test_recommend_filters_training_and_ranks_ready_first(tmp_path: Path) -> None:
    scout, db = make_scout(
        tmp_path,
        [
            make_candidate("ready", free_requests_per_day=50),
            make_candidate("training", trains_on_data=True),
            make_candidate("other-capability", capabilities=("image",)),
            make_candidate("needs-key", api_key_env="MISSING_KEY"),
        ],
        environ={"TEST_CANDIDATE_KEY": "secret"},
    )
    try:
        cards = scout.recommend(ScoutQuery(capability="llm", allow_training_data=False))
    finally:
        db.close()
    assert [card.provider_id for card in cards] == ["ready", "needs-key"]
    assert cards[0].status == "ready"
    assert cards[1].status == "needs_key"


def test_training_and_configured_candidates_are_opt_in(tmp_path: Path) -> None:
    registry = CloudRegistry({"configured": make_candidate("configured")})  # type: ignore[dict-item]
    scout, db = make_scout(
        tmp_path,
        [make_candidate("ready", trains_on_data=True), make_candidate("configured")],
        providers=registry,
        environ={},
    )
    try:
        assert scout.recommend(ScoutQuery(allow_training_data=False)) == ()
        cards = scout.recommend(ScoutQuery(allow_training_data=True, include_configured=True))
    finally:
        db.close()
    assert {card.provider_id for card in cards} == {"ready", "configured"}


def test_restricted_data_requires_private_policy(tmp_path: Path) -> None:
    scout, db = make_scout(
        tmp_path,
        [
            make_candidate("public-only"),
            make_candidate("private-ok", data_policy="private_allowed"),
        ],
    )
    try:
        cards = scout.recommend(ScoutQuery(data_class="private"))
    finally:
        db.close()
    assert [card.provider_id for card in cards] == ["private-ok"]


def test_unknown_data_class_is_rejected() -> None:
    with pytest.raises(ScoutError, match="unknown data class"):
        ScoutQuery(data_class="health")  # type: ignore[arg-type]


def test_watermark_may_is_refused_when_not_allowed(tmp_path: Path) -> None:
    scout, db = make_scout(
        tmp_path,
        [
            make_candidate("maybe-watermark", watermark="may"),
            make_candidate("clean", watermark="none"),
        ],
    )
    try:
        cards = scout.recommend(ScoutQuery(allow_watermark=False))
    finally:
        db.close()
    assert [card.provider_id for card in cards] == ["clean"]


def test_card_never_prints_the_secret(tmp_path: Path) -> None:
    scout, db = make_scout(
        tmp_path,
        [make_candidate("ready")],
        environ={"TEST_CANDIDATE_KEY": "super-secret-value"},
    )
    try:
        card = scout.recommend()[0]
    finally:
        db.close()
    rendered = card.to_text() + repr(card.to_dict()) + repr(card)
    assert "super-secret-value" not in rendered
    assert "TEST_CANDIDATE_KEY (present)" in rendered


def test_no_candidate_saves_a_local_handoff(tmp_path: Path) -> None:
    scout, db = make_scout(tmp_path, [make_candidate("training", trains_on_data=True)])
    try:
        outcome = scout.on_exhausted(
            ScoutQuery(allow_training_data=False),
            goal="finish the report",
            plan=["write final"],
            completed_steps=["read draft"],
            artifacts=["data/report.md"],
            criteria=["report is complete"],
            constraints=["public data only"],
            next_step="write the conclusion",
            task_id="task-1",
        )
        assert outcome.found is False
        assert outcome.handoff is not None
        assert outcome.handoff_id == 1
        pack = load_handoff(db, outcome.handoff_id)
        rows = db.recent_handoffs()
        audit = db.recent_audit()[0]
    finally:
        db.close()
    assert isinstance(pack, HandoffPack)
    assert pack is not None
    assert pack.goal == "finish the report"
    assert pack.completed_steps == ["read draft"]
    assert pack.next_step == "write the conclusion"
    assert "finish the report" in pack.to_prompt()
    assert rows[0]["task_id"] == "task-1"
    assert audit["action"] == "cloud.scout"
    assert audit["decision"] == "handoff"


def test_found_candidate_still_saves_checkpoint(tmp_path: Path) -> None:
    scout, db = make_scout(tmp_path, [make_candidate("ready")])
    try:
        outcome = scout.on_exhausted(ScoutQuery(), goal="do the task")
        rows = db.recent_handoffs()
        audit = db.recent_audit()[0]
    finally:
        db.close()
    assert outcome.found is True
    assert outcome.handoff is not None
    assert outcome.handoff_id == 1
    assert rows[0]["goal"] == "do the task"
    assert audit["decision"] == "allow"


def test_approve_appends_a_valid_provider(tmp_path: Path) -> None:
    registry_path = tmp_path / "cloud.yaml"
    registry_path.write_text(CLOUD_YAML, encoding="utf-8")
    registry = load_cloud_registry(registry_path)
    scout, db = make_scout(
        tmp_path,
        [make_candidate("new-provider", api_key_env="NEW_PROVIDER_KEY")],
        providers=registry,
    )
    try:
        provider = scout.approve("new-provider", registry_path)
        reloaded = load_cloud_registry(registry_path)
        audit = db.recent_audit()[0]
    finally:
        db.close()
    assert provider.id == "new-provider"
    assert reloaded.get("new-provider") is not None
    assert reloaded.get("existing") is not None
    assert "NEW_PROVIDER_KEY" in registry_path.read_text(encoding="utf-8")
    assert audit["action"] == "cloud.scout.approve"


def test_approve_refuses_duplicates_and_keyless(tmp_path: Path) -> None:
    registry_path = tmp_path / "cloud.yaml"
    registry_path.write_text(CLOUD_YAML, encoding="utf-8")
    scout, db = make_scout(
        tmp_path,
        [
            make_candidate("existing"),
            make_candidate("keyless", api_key_env=""),
        ],
        providers=load_cloud_registry(registry_path),
    )
    try:
        with pytest.raises(ScoutError, match="already configured"):
            scout.approve("existing", registry_path)
        with pytest.raises(ScoutError, match="keyless"):
            scout.approve("keyless", registry_path)
        with pytest.raises(ScoutError, match="unknown"):
            scout.approve("missing", registry_path)
    finally:
        db.close()


def test_catalog_loader_rejects_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "candidates.yaml"
    path.write_text(
        "candidates:\n"
        "  - id: one\n"
        "    name: One\n"
        "    capabilities: [llm]\n"
        "    base_url: https://example.test/v1\n"
        "    api_key_env: ONE_KEY\n"
        "    free_requests_per_day: 1\n"
        "    mystery: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ScoutError, match="unknown candidate keys"):
        load_scout_registry(path)


def test_catalog_loader_accepts_type_shorthand(tmp_path: Path) -> None:
    path = tmp_path / "candidates.yaml"
    path.write_text(
        "candidates:\n"
        "  - id: one\n"
        "    name: One\n"
        "    type: embedding\n"
        "    base_url: https://example.test/v1\n"
        "    api_key_env: ONE_KEY\n"
        "    free_tokens_per_day: 1000\n",
        encoding="utf-8",
    )
    registry = load_scout_registry(path)
    assert registry.require("one").capabilities == ("embedding",)


def test_catalog_loader_rejects_unknown_root_keys(tmp_path: Path) -> None:
    path = tmp_path / "candidates.yaml"
    path.write_text(
        "candidates:\n"
        "  - id: one\n"
        "    name: One\n"
        "    type: llm\n"
        "    base_url: https://example.test/v1\n"
        "    api_key_env: ONE_KEY\n"
        "    free_requests_per_day: 1\n"
        "extra: true\n",
        encoding="utf-8",
    )
    with pytest.raises(ScoutError, match="unknown catalog keys"):
        load_scout_registry(path)


def test_catalog_loader_rejects_non_string_capabilities(tmp_path: Path) -> None:
    path = tmp_path / "candidates.yaml"
    path.write_text(
        "candidates:\n"
        "  - id: one\n"
        "    name: One\n"
        "    capabilities: [llm, 42]\n"
        "    base_url: https://example.test/v1\n"
        "    api_key_env: ONE_KEY\n"
        "    free_requests_per_day: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(ScoutError, match="capabilities must be strings"):
        load_scout_registry(path)
