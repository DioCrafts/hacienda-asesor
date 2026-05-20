from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from hacienda_gpt.decision.schemas import CaseState, Fact, FactValueType
from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore


def _build_case(case_id: str, user_id: str, updated_at: datetime) -> CaseState:
    return CaseState(
        case_id=case_id,
        user_id=user_id,
        jurisdiction="ES",
        tax_period="2025",
        facts=[
            Fact(
                fact_id=f"fact_{case_id}",
                name="residencia_fiscal",
                value="ES",
                value_type=FactValueType.STRING,
                source="user_input",
                confidence=0.9,
                updated_at=updated_at,
            )
        ],
        created_at=updated_at,
        updated_at=updated_at,
    )


def test_save_and_get_case_roundtrip(tmp_path: pytest.TempPathFactory) -> None:
    db = tmp_path / "state.db"
    store = SQLiteCaseStateStore(str(db))
    now = datetime.now(UTC)
    case = _build_case("case_1", "user_1", now)

    store.save_case(case)
    fetched = store.get_case("case_1")

    assert fetched is not None
    assert fetched.case_id == case.case_id
    assert fetched.user_id == case.user_id
    assert fetched.facts[0].name == "residencia_fiscal"


def test_save_case_upsert_updates_existing_record(tmp_path: pytest.TempPathFactory) -> None:
    db = tmp_path / "state.db"
    store = SQLiteCaseStateStore(str(db))
    t1 = datetime.now(UTC)
    t2 = t1 + timedelta(minutes=2)

    store.save_case(_build_case("case_1", "user_1", t1))
    updated_case = _build_case("case_1", "user_1", t2)
    store.save_case(updated_case)

    fetched = store.get_case("case_1")
    assert fetched is not None
    assert fetched.updated_at == t2


def test_list_cases_by_user_sorted_desc(tmp_path: pytest.TempPathFactory) -> None:
    db = tmp_path / "state.db"
    store = SQLiteCaseStateStore(str(db))
    base = datetime.now(UTC)

    store.save_case(_build_case("case_old", "user_1", base))
    store.save_case(_build_case("case_new", "user_1", base + timedelta(minutes=1)))
    store.save_case(_build_case("case_other_user", "user_2", base + timedelta(minutes=3)))

    cases = store.list_cases("user_1")

    assert [case.case_id for case in cases] == ["case_new", "case_old"]


def test_append_audit_event_and_retrieve_sequence(tmp_path: pytest.TempPathFactory) -> None:
    db = tmp_path / "state.db"
    store = SQLiteCaseStateStore(str(db))
    now = datetime.now(UTC)
    store.save_case(_build_case("case_1", "user_1", now))

    store.append_audit_event("case_1", {"event_type": "case_created", "actor": "system"})
    store.append_audit_event("case_1", {"event_type": "question_asked", "actor": "assistant"})

    events = store.list_audit_events("case_1")
    assert len(events) == 2
    assert events[0]["event_type"] == "case_created"
    assert events[1]["event_type"] == "question_asked"
    assert "event_time" in events[0]


def test_append_audit_event_unknown_case_raises(tmp_path: pytest.TempPathFactory) -> None:
    db = tmp_path / "state.db"
    store = SQLiteCaseStateStore(str(db))

    with pytest.raises(KeyError):
        store.append_audit_event("unknown_case", {"event_type": "x"})


def test_basic_concurrency_with_parallel_upserts(tmp_path: pytest.TempPathFactory) -> None:
    db = tmp_path / "state.db"
    store = SQLiteCaseStateStore(str(db))
    base = datetime.now(UTC)

    def worker(index: int) -> None:
        case = _build_case(f"case_{index}", "user_concurrent", base + timedelta(seconds=index))
        store.save_case(case)
        store.append_audit_event(case.case_id, {"event_type": "saved", "worker": index})

    with ThreadPoolExecutor(max_workers=6) as executor:
        for idx in range(20):
            executor.submit(worker, idx)

    cases = store.list_cases("user_concurrent")
    assert len(cases) == 20
    events = store.list_audit_events("case_0")
    assert len(events) == 1
    assert events[0]["event_type"] == "saved"
