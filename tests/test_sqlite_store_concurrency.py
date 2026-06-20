"""Concurrency tests for request-scoped SQLite transaction state."""

from __future__ import annotations

import threading

from repositories.sqlite_store import SqliteContactStore


def test_concurrent_transactions_use_distinct_connections(tmp_path):
    db_path = tmp_path / "conc.db"
    store = SqliteContactStore(db_path)
    store.init_db()

    conn_ids: list[int] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def worker(company_name: str) -> None:
        try:
            with store.transaction():
                active = store._kwargs().get("conn")
                assert active is not None
                conn_ids.append(id(active))
                barrier.wait()
                store.upsert_lead({"company_name": company_name})
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("Company A",)),
        threading.Thread(target=worker, args=("Company B",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    assert len(conn_ids) == 2
    assert conn_ids[0] != conn_ids[1]
    company_names = {lead["company_name"] for lead in store.list_leads()}
    assert company_names == {"Company A", "Company B"}


def test_uncommitted_writes_not_visible_outside_transaction(tmp_path):
    db_path = tmp_path / "conc.db"
    store = SqliteContactStore(db_path)
    store.init_db()

    in_tx = threading.Event()
    release = threading.Event()
    visible_counts: list[int] = []
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            with store.transaction():
                store.upsert_lead({"company_name": "Uncommitted Writer Co"})
                in_tx.set()
                release.wait()
        except BaseException as exc:
            errors.append(exc)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    in_tx.wait()
    try:
        visible_counts.append(store.count_potential_clients())
    except BaseException as exc:
        errors.append(exc)
    release.set()
    writer_thread.join()

    assert not errors
    assert visible_counts == [0]
    assert store.count_potential_clients() == 1


def test_concurrent_transactions_both_commit(tmp_path):
    db_path = tmp_path / "conc.db"
    store = SqliteContactStore(db_path)
    store.init_db()

    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def worker(company_name: str) -> None:
        try:
            with store.transaction():
                barrier.wait()
                lead, _ = store.upsert_lead({"company_name": company_name})
                store.add_task(
                    lead["id"],
                    {"title": f"Task for {company_name}", "status": "open"},
                )
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("Alpha Co",)),
        threading.Thread(target=worker, args=("Beta Co",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors
    company_names = {lead["company_name"] for lead in store.list_leads()}
    assert company_names == {"Alpha Co", "Beta Co"}
    assert store.count_potential_clients() == 2
