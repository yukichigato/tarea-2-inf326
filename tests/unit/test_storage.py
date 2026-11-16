import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shortener.domain import INITIAL_ID, UrlHit, encode_base62
from shortener.storage import SQLiteHitReader, SQLiteHitRepository, SQLiteURLRepository


def test_first_identifier_and_increment(tmp_path: Path) -> None:
    repository = SQLiteURLRepository(tmp_path / "urls.sqlite3")

    first = repository.create("https://usm.cl/first")
    second = repository.create("https://usm.cl/second")

    assert first.id == INITIAL_ID
    assert first.hash == encode_base62(INITIAL_ID)
    assert second.id == INITIAL_ID + 1
    assert repository.get(first.hash) == first


def test_records_survive_reopening_and_sequence_does_not_reset(tmp_path: Path) -> None:
    path = tmp_path / "urls.sqlite3"
    first = SQLiteURLRepository(path).create("https://usm.cl/first")
    reopened = SQLiteURLRepository(path)

    assert reopened.get(first.hash) == first
    assert reopened.create("https://usm.cl/second").id == first.id + 1


def test_concurrent_insertions_have_distinct_sequential_identifiers(tmp_path: Path) -> None:
    repository = SQLiteURLRepository(tmp_path / "urls.sqlite3")

    with ThreadPoolExecutor(max_workers=8) as executor:
        records = list(executor.map(repository.create, [f"https://usm.cl/{i}" for i in range(40)]))

    assert len({record.hash for record in records}) == 40
    assert sorted(record.id for record in records) == list(range(INITIAL_ID, INITIAL_ID + 40))


def test_unknown_hash_returns_none(tmp_path: Path) -> None:
    repository = SQLiteURLRepository(tmp_path / "urls.sqlite3")

    assert repository.get("missing") is None
    assert repository.get("' OR 1=1 --") is None


def test_hits_store_the_hash_and_the_reader_groups_them(tmp_path: Path) -> None:
    path = tmp_path / "hits.sqlite3"
    repository = SQLiteHitRepository(path)
    first = datetime(2026, 11, 4, 12, tzinfo=UTC)
    second = datetime(2026, 11, 4, 13, tzinfo=UTC)
    for hit in (
        UrlHit("eRgfS", "https://usm.cl/admision", first),
        UrlHit("eRgfT", "https://usm.cl/admision", first),
        UrlHit("eRgfS", "https://usm.cl/admision", second),
    ):
        repository.record(hit)
    reader = SQLiteHitReader(path)

    totals = [tuple(row) for row in reader.visits_by_hash()]
    assert totals == [
        ("eRgfS", "https://usm.cl/admision", 2, second.isoformat()),
        ("eRgfT", "https://usm.cl/admision", 1, first.isoformat()),
    ]
    assert [row["hash"] for row in reader.latest(2)] == ["eRgfS", "eRgfT"]


def test_reader_never_writes_to_the_log_db(tmp_path: Path) -> None:
    with pytest.raises(sqlite3.OperationalError):
        SQLiteHitReader(tmp_path / "missing.sqlite3").latest(1)
    assert not (tmp_path / "missing.sqlite3").exists()


def test_log_db_created_before_the_hash_field_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "hits.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE hits (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "url TEXT NOT NULL, visited_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO hits(url, visited_at) VALUES ('https://usm.cl/', '2026-11-04T12:00:00+00:00')"
        )

    repository = SQLiteHitRepository(path)
    repository.record(UrlHit("eRgfR", "https://usm.cl/", datetime(2026, 11, 5, tzinfo=UTC)))

    with closing(sqlite3.connect(path)) as connection:
        rows = connection.execute("SELECT hash, url FROM hits ORDER BY id").fetchall()
    assert rows == [(None, "https://usm.cl/"), ("eRgfR", "https://usm.cl/")]
