import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from shortener.domain import INITIAL_ID, ShortURL, UrlHit, encode_base62


class SQLiteDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            with connection:
                yield connection


class SQLiteURLRepository(SQLiteDatabase):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        with self.connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS urls ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "hash TEXT UNIQUE, long_url TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO sqlite_sequence(name, seq) "
                "SELECT 'urls', ? WHERE NOT EXISTS "
                "(SELECT 1 FROM sqlite_sequence WHERE name = 'urls')",
                (INITIAL_ID - 1,),
            )
            connection.execute(
                "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'urls'",
                (INITIAL_ID - 1,),
            )

    def create(self, long_url: str) -> ShortURL:
        with self.connection() as connection:
            cursor = connection.execute("INSERT INTO urls(long_url) VALUES (?)", (long_url,))
            record_id = cursor.lastrowid
            if record_id is None:
                raise RuntimeError("Database did not return an identifier")
            code = encode_base62(record_id)
            connection.execute("UPDATE urls SET hash = ? WHERE id = ?", (code, record_id))
        return ShortURL(id=record_id, hash=code, long_url=long_url)

    def get(self, code: str) -> ShortURL | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT id, hash, long_url FROM urls WHERE hash = ?", (code,)
            ).fetchone()
        return ShortURL(**dict(row)) if row is not None else None

    def ping(self) -> None:
        with self.connection() as connection:
            connection.execute("SELECT 1 FROM urls LIMIT 1").fetchone()


class SQLiteHitRepository(SQLiteDatabase):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        with self.connection() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS hits ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "url TEXT NOT NULL, visited_at TEXT NOT NULL, hash TEXT NOT NULL)"
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(hits)")}
            if "hash" not in columns:
                # A Log DB created before the event carried the hash keeps its earlier
                # visits, which have no hash (NULL); new visits always store one.
                connection.execute("ALTER TABLE hits ADD COLUMN hash TEXT")
            connection.execute("CREATE INDEX IF NOT EXISTS hits_hash ON hits(hash)")

    def record(self, hit: UrlHit) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO hits(hash, url, visited_at) VALUES (?, ?, ?)",
                (hit.hash, hit.url, hit.visited_at.isoformat()),
            )


class SQLiteHitReader:
    """Read-only queries on the Log DB, used to inspect the recorded visits."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=10)) as connection:
            connection.row_factory = sqlite3.Row
            yield connection

    def visits_by_hash(self) -> list[sqlite3.Row]:
        with self.connection() as connection:
            return connection.execute(
                "SELECT hash, url, COUNT(*) AS visits, MAX(visited_at) AS last_visit "
                "FROM hits GROUP BY hash, url ORDER BY visits DESC, hash"
            ).fetchall()

    def latest(self, limit: int) -> list[sqlite3.Row]:
        with self.connection() as connection:
            return connection.execute(
                "SELECT hash, url, visited_at FROM hits ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
