"""
db.py – Datenbankzugriff für den RTC_IncidentReportBot
"""

import os
import logging
import mariadb
from contextlib import contextmanager

logger = logging.getLogger(__name__)


def get_connection():
    return mariadb.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 3306)),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )


@contextmanager
def db_cursor():
    conn = get_connection()
    try:
        cur = conn.cursor(dictionary=True)
        yield cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ─── Rennkalender ─────────────────────────────────────────────────────────────

def get_race_for_date(race_date) -> dict | None:
    """
    Gibt das Rennen für ein bestimmtes Datum zurück, sofern track_id nicht NULL ist
    und die aktive Season kein Fun-Event ist.
    Gibt None zurück, wenn kein (wertbares) Rennen stattfindet.
    """
    with db_cursor() as cur:
        cur.execute("""
            SELECT rc.race_id, rc.race_number, rc.race_date, rc.track_id,
                   rc.laps, t.name AS track_name, s.fun_event
            FROM race_calendar rc
            JOIN seasons s ON s.is_active = 1
            LEFT JOIN tracks t ON t.track_id = rc.track_id
            WHERE rc.race_date = ?
              AND rc.track_id IS NOT NULL
              AND s.fun_event = 0
        """, (race_date,))
        return cur.fetchone()


# ─── Fahrer & Grids ───────────────────────────────────────────────────────────

def get_driver_grid_for_race(race_id: int, discord_id: str, discord_nick: str) -> dict | None:
    """
    Ermittelt das Grid eines Fahrers anhand seiner Discord-ID (Fallback: Nickname).
    Gibt dict mit grid_id, grid_name zurück oder None.
    """
    with db_cursor() as cur:
        # Versuch 1: Discord ID
        cur.execute("""
            SELECT g.grid_id, g.name AS grid_name
            FROM race_results rr
            JOIN grids g ON g.grid_id = rr.grid_id
            JOIN drivers d ON d.driver_id = rr.driver_id
            WHERE rr.race_id = ?
              AND d.discord_id = ?
            LIMIT 1
        """, (race_id, discord_id))
        row = cur.fetchone()
        if row:
            return row

        # Fallback: Discord-Nickname
        cur.execute("""
            SELECT g.grid_id, g.name AS grid_name
            FROM race_results rr
            JOIN grids g ON g.grid_id = rr.grid_id
            JOIN drivers d ON d.driver_id = rr.driver_id
            WHERE rr.race_id = ?
              AND d.discord_name = ?
            LIMIT 1
        """, (race_id, discord_nick))
        return cur.fetchone()


def get_drivers_in_grid(race_id: int, grid_id: int) -> list[dict]:
    """
    Gibt alle Fahrer (driver_id, psn_name) zurück, die in einem bestimmten
    Grid eines Rennens gefahren sind.
    """
    with db_cursor() as cur:
        cur.execute("""
            SELECT d.driver_id, d.psn_name
            FROM race_results rr
            JOIN drivers d ON d.driver_id = rr.driver_id
            WHERE rr.race_id = ?
              AND rr.grid_id = ?
            ORDER BY rr.finish_position
        """, (race_id, grid_id))
        return cur.fetchall()


def get_psn_name(discord_id: str, discord_nick: str) -> str | None:
    """Gibt den PSN-Namen eines Fahrers zurück."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT psn_name FROM drivers WHERE discord_id = ? LIMIT 1",
            (discord_id,)
        )
        row = cur.fetchone()
        if row:
            return row["psn_name"]

        cur.execute(
            "SELECT psn_name FROM drivers WHERE discord_name = ? LIMIT 1",
            (discord_nick,)
        )
        row = cur.fetchone()
        return row["psn_name"] if row else None
