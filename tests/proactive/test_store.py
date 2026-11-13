from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from hacienda_gpt.proactive.store import Notification, NotificationStore


def _n(key: str, case_id: str = "c1", **extra) -> Notification:
    fields = {"kind": "plazo", "title": "Título", "body": "Cuerpo", "severity": "aviso", **extra}
    return Notification(case_id=case_id, key=key, **fields)


def test_add_dedupes_per_case_and_key(tmp_path) -> None:
    store = NotificationStore(str(tmp_path / "db.sqlite3"))
    first = _n("k1", amount=Decimal("538.50"), due_date=date(2026, 12, 31), actions=["Haz X"], sources=["art. 1"])

    assert store.add(first) is True
    assert store.add(_n("k1", title="Otro título")) is False
    assert store.add(_n("k1", case_id="c2")) is True  # same key, another case

    (stored,) = store.list("c1")
    assert stored == first  # round-trips every field, including Decimal and date
    assert stored.title == "Título" and stored.amount == Decimal("538.50")


def test_list_is_newest_first_and_read_state(tmp_path) -> None:
    store = NotificationStore(str(tmp_path / "db.sqlite3"))
    base = datetime(2026, 11, 1, tzinfo=UTC)
    old, new = _n("old", created_at=base), _n("new", created_at=base + timedelta(days=1))
    store.add(old)
    store.add(new)

    assert [n.key for n in store.list("c1")] == ["new", "old"]
    assert store.count_unread("c1") == 2 and store.count_unread("other") == 0

    assert store.mark_read("c1", old.id) is True
    assert store.mark_read("c2", new.id) is False  # wrong case
    assert store.mark_read("c1", "missing") is False
    assert store.count_unread("c1") == 1
    assert [n.key for n in store.list("c1", unread_only=True)] == ["new"]

    read_at = store.list("c1")[1].read_at
    assert read_at is not None
    assert store.mark_read("c1", old.id) is True  # idempotent, keeps the first timestamp
    assert store.list("c1")[1].read_at == read_at


def test_store_persists_across_instances_and_removes_superseded(tmp_path) -> None:
    path = str(tmp_path / "db.sqlite3")
    store = NotificationStore(path)
    for key in ("fin:2026:a", "fin:2026:b", "fin:2025:a", "plazo:x"):
        store.add(_n(key))
    store.close()

    reopened = NotificationStore(path)
    assert reopened.remove_superseded("c1", key_prefix="fin:2026:", keep_key="fin:2026:b") == 1
    assert sorted(n.key for n in reopened.list("c1")) == ["fin:2025:a", "fin:2026:b", "plazo:x"]
