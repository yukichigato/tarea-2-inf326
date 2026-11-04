from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from shortener.domain import INITIAL_ID, encode_base62
from shortener.storage import SQLiteURLRepository


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
