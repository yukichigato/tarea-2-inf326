"""Print the url hits recorded by the Log service of architecture 1.

Run it inside the Log container, which holds the Log DB:

    docker compose exec log python -m shortener.hits_report [--limit N]
"""

import argparse
import sqlite3

from shortener.settings import Settings
from shortener.storage import SQLiteHitReader


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Show the url hits recorded by Log.")
    parser.add_argument(
        "--limit", type=positive_int, default=20, help="latest visits to list (default: 20)"
    )
    arguments = parser.parse_args(argv)
    database_path = Settings.from_env().database_path
    reader = SQLiteHitReader(database_path)
    try:
        totals = reader.visits_by_hash()
        latest = reader.latest(arguments.limit)
    except sqlite3.Error as error:
        raise SystemExit(f"Could not read the Log DB at {database_path}: {error}") from error

    print("Visits per short URL:")
    print(f"  {'hash':<12} {'visits':>6}  {'last visit (UTC)':<32} url")
    for row in totals:
        print(
            f"  {row['hash'] or '(none)':<12} {row['visits']:>6}  {row['last_visit']:<32} "
            f"{row['url']}"
        )
    print(f"\nLatest {len(latest)} visits:")
    for row in latest:
        print(f"  {row['visited_at']:<32} {row['hash'] or '(none)':<12} {row['url']}")


if __name__ == "__main__":
    main()
