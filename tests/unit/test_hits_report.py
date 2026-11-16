from datetime import UTC, datetime
from pathlib import Path

import pytest

from shortener.domain import UrlHit
from shortener.hits_report import main
from shortener.storage import SQLiteHitRepository


def test_report_lists_visits_per_short_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "hits.sqlite3"
    repository = SQLiteHitRepository(path)
    for code in ("eRgfS", "eRgfS", "eRgfT"):
        repository.record(UrlHit(code, "https://usm.cl/admision", datetime.now(UTC)))
    monkeypatch.setenv("DATABASE_PATH", str(path))

    main(["--limit", "2"])

    output = capsys.readouterr().out
    totals, latest = output.split("Latest")
    assert "eRgfS" in totals and "      2  " in totals
    assert "eRgfT" in totals and "      1  " in totals
    assert latest.startswith(" 2 visits:")
    assert latest.count("https://usm.cl/admision") == 2


def test_report_explains_a_missing_log_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "missing.sqlite3"))

    with pytest.raises(SystemExit, match="Could not read the Log DB"):
        main([])


def test_report_rejects_a_non_positive_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit):
        main(["--limit", "0"])
