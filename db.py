"""Local SQLite archive of meter readings.

Holds what the EAC API has already returned, so the dashboard can render without
a live token and without re-fetching on every interaction. Two ideas run through
this module:

- **Scope everything by user.** `user_id` is the JWT's `sub` claim (see
  `EacClient.user_id`) and is part of every primary key. Nothing here is
  queryable without it, so one account's rows can't be served to another.
- **Ingest raw, derive on read.** Rows are stored exactly as the API returned
  them - no rounding, no gap-filling, no derived columns. `docs/channels.md`
  documents several places where the "obvious" derivation is wrong, so those
  decisions belong at read time where they can be changed.
"""
import datetime as dt
import os
import sqlite3

import pandas as pd

DB_PATH = os.environ.get("EAC_DB_PATH", "data/eac.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    user_id     TEXT NOT NULL,
    sp_id       TEXT NOT NULL,
    mc_id       TEXT NOT NULL,
    ts          TEXT NOT NULL,  -- ISO 'YYYY-MM-DDTHH:MM:SS', midnight for daily channels
    reading     REAL,           -- cumulative meter value; NULL on load-profile channels
    consumption REAL NOT NULL,  -- server-computed delta for the interval
    PRIMARY KEY (user_id, sp_id, mc_id, ts)
);

CREATE TABLE IF NOT EXISTS sync_state (
    user_id     TEXT NOT NULL,
    sp_id       TEXT NOT NULL,
    mc_id       TEXT NOT NULL,
    synced_from TEXT NOT NULL,  -- oldest date requested so far (not necessarily returned)
    synced_to   TEXT NOT NULL,  -- newest date requested so far
    last_sync   TEXT NOT NULL,
    PRIMARY KEY (user_id, sp_id, mc_id)
);
"""


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    """Open the database, creating the file and schema if needed."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    # WAL lets the dashboard read while a sync writes, instead of blocking on it.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    return conn


def _to_ts(value) -> str:
    """Normalise a date or datetime to the single ISO format used in `ts`.

    Daily channels report a `datetime.date` and load-profile channels a full
    timestamp; storing both as 'YYYY-MM-DDTHH:MM:SS' keeps ordering and BETWEEN
    range queries correct across channel types.
    """
    return pd.Timestamp(value).strftime("%Y-%m-%dT%H:%M:%S")


def upsert_readings(
    conn: sqlite3.Connection, user_id: str, sp_id: str, mc_id: str, df: pd.DataFrame
) -> int:
    """Insert readings, overwriting any row already stored for the same timestamp.

    `df` must come from `EacClient.get_readings(..., truncate_to_date=False)`,
    for every channel including the daily ones: `ts` stores the raw interval-end
    stamp, and `fetch_readings` is what converts it to the day measured. Passing
    an already-truncated frame shifts the same day off twice.

    Overwriting rather than ignoring is deliberate: EAC publishes days late and
    revises values afterwards (docs/channels.md), so a re-sync of an old window
    is how corrections arrive. Returns the number of rows written.
    """
    if df.empty:
        return 0

    rows = [
        (
            user_id,
            sp_id,
            mc_id,
            _to_ts(row.date),
            None if pd.isna(row.reading) else float(row.reading),
            float(row.consumption),
        )
        for row in df.itertuples()
    ]
    with conn:
        conn.executemany(
            """
            INSERT INTO readings (user_id, sp_id, mc_id, ts, reading, consumption)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, sp_id, mc_id, ts) DO UPDATE SET
                reading     = excluded.reading,
                consumption = excluded.consumption
            """,
            rows,
        )
    return len(rows)


def fetch_readings(
    conn: sqlite3.Connection,
    user_id: str,
    sp_id: str,
    mc_id: str,
    start_date,
    end_date,
    truncate_to_date: bool = True,
) -> pd.DataFrame:
    """Read back a window, shaped exactly like `EacClient.get_readings`.

    A reading stamped `ts` measures the interval *ending* at `ts`
    (docs/channels.md), so it belongs to this window when the interval it
    covers starts inside it - hence the range is open at the bottom and closed
    at the top. For a daily channel that means consumption for `end_date`,
    which carries a timestamp of `end_date + 1 day` at midnight, is included;
    filtering on the plain dates would drop the last day of every window.

    `EacClient.get_readings` applies the same bounds - it requests a day of
    slack on each side and filters, because the API's own window drifts with
    daylight saving - so both sources return the same rows for the same
    request. Truncated dates likewise label a row with the day it measures
    (`ts` minus a day), not the midnight it is stamped at.
    """
    start = _to_ts(pd.Timestamp(start_date).normalize())
    end = _to_ts(pd.Timestamp(end_date).normalize() + pd.Timedelta(days=1))
    df = pd.read_sql_query(
        """
        SELECT ts AS date, reading, consumption
        FROM readings
        WHERE user_id = ? AND sp_id = ? AND mc_id = ? AND ts > ? AND ts <= ?
        ORDER BY ts
        """,
        conn,
        params=(user_id, sp_id, mc_id, start, end),
    )
    if df.empty:
        return pd.DataFrame(columns=["date", "reading", "consumption"])

    df["date"] = pd.to_datetime(df["date"])
    if truncate_to_date:
        df["date"] = (df["date"] - pd.Timedelta(days=1)).dt.date
    return df.reset_index(drop=True)


def get_sync_state(
    conn: sqlite3.Connection, user_id: str, sp_id: str, mc_id: str
) -> dict | None:
    """Return the window already requested for this channel, or None if never synced."""
    row = conn.execute(
        """
        SELECT synced_from, synced_to, last_sync FROM sync_state
        WHERE user_id = ? AND sp_id = ? AND mc_id = ?
        """,
        (user_id, sp_id, mc_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "synced_from": dt.date.fromisoformat(row[0]),
        "synced_to": dt.date.fromisoformat(row[1]),
        "last_sync": row[2],
    }


def record_sync(
    conn: sqlite3.Connection, user_id: str, sp_id: str, mc_id: str, start_date, end_date
) -> None:
    """Widen the recorded sync window for a channel.

    Tracks what was *requested*, not what came back - a request that returned
    nothing because EAC hasn't published yet still needs re-requesting later,
    and without this the caller can't tell that case apart from a real gap.
    """
    start = pd.Timestamp(start_date).date().isoformat()
    end = pd.Timestamp(end_date).date().isoformat()
    with conn:
        conn.execute(
            """
            INSERT INTO sync_state (user_id, sp_id, mc_id, synced_from, synced_to, last_sync)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, sp_id, mc_id) DO UPDATE SET
                synced_from = MIN(synced_from, excluded.synced_from),
                synced_to   = MAX(synced_to,   excluded.synced_to),
                last_sync   = excluded.last_sync
            """,
            (user_id, sp_id, mc_id, start, end, dt.datetime.now().isoformat(timespec="seconds")),
        )


def delete_user(conn: sqlite3.Connection, user_id: str) -> None:
    """Erase everything held for one account (for a deletion request)."""
    with conn:
        conn.execute("DELETE FROM readings WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM sync_state WHERE user_id = ?", (user_id,))
