"""
db.py – Datenbankzugriff für den RTC_IncidentReportBot
"""

import os
import logging
import pymysql
from contextlib import contextmanager

logger = logging.getLogger(__name__)


def get_connection():
    return pymysql.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 3306)),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        cursorclass=pymysql.cursors.DictCursor,
        charset="utf8mb4",
    )


@contextmanager
def db_cursor():
    conn = get_connection()
    try:
        cur = conn.cursor()
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
    Gibt das Rennen für ein bestimmtes Datum zurück, sofern is_pause=0
    und die aktive Season kein Fun-Event ist.
    Gibt die echte race_id aus der races-Tabelle zurück.
    """
    with db_cursor() as cur:
        cur.execute("""
            SELECT r.race_id, rc.race_number, rc.race_date,
                   rc.track_name, rc.laps, s.fun_event
            FROM race_calendar rc
            JOIN seasons s ON s.is_active = 1
            JOIN races r ON r.race_date = rc.race_date AND r.season_id = s.season_id
            WHERE rc.race_date = %s
              AND rc.is_pause = 0
              AND s.fun_event = 0
        """, (race_date,))
        return cur.fetchone()


# ─── Fahrer & Grids ───────────────────────────────────────────────────────────

def get_driver_grid_for_race(race_id: int, discord_id: str, discord_nick: str) -> dict | None:
    """
    Ermittelt das Grid eines Fahrers anhand seiner Discord-ID (Fallback: Nickname).
    Gibt dict mit grid_id, grid_name zurück oder None.
    """
    logger.info(f"get_driver_grid_for_race: race_id={race_id!r}, discord_id={discord_id!r}, discord_nick={discord_nick!r}")
    with db_cursor() as cur:
        # Debug: alle Fahrer in diesem Rennen
        cur.execute("""
            SELECT d.driver_id, d.psn_name, d.discord_id, d.discord_name, rr.grid_id
            FROM race_results rr
            JOIN drivers d ON d.driver_id = rr.driver_id
            JOIN grids g ON g.grid_id = rr.grid_id
            WHERE g.race_id = %s
        """, (race_id,))
        all_drivers = cur.fetchall()
        logger.info(f"Fahrer in race_id={race_id}: {all_drivers}")

        # Versuch 1: Discord ID
        cur.execute("""
            SELECT g.grid_id, g.grid_label AS grid_name
            FROM race_results rr
            JOIN grids g ON g.grid_id = rr.grid_id
            JOIN drivers d ON d.driver_id = rr.driver_id
            WHERE g.race_id = %s
              AND d.discord_id = %s
            LIMIT 1
        """, (race_id, discord_id))
        row = cur.fetchone()
        logger.info(f"Treffer discord_id={discord_id!r}: {row}")
        if row:
            return row

        # Fallback: Discord-Nickname
        cur.execute("""
            SELECT g.grid_id, g.grid_label AS grid_name
            FROM race_results rr
            JOIN grids g ON g.grid_id = rr.grid_id
            JOIN drivers d ON d.driver_id = rr.driver_id
            WHERE g.race_id = %s
              AND d.discord_name = %s
            LIMIT 1
        """, (race_id, discord_nick))
        row = cur.fetchone()
        logger.info(f"Treffer discord_nick={discord_nick!r}: {row}")
        return row


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
            WHERE rr.race_id = %s
              AND rr.grid_id = %s
            ORDER BY rr.finish_pos_grid
        """, (race_id, grid_id))
        return cur.fetchall()


def get_psn_name(discord_id: str, discord_nick: str) -> str | None:
    """Gibt den PSN-Namen eines Fahrers zurück."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT psn_name FROM drivers WHERE discord_id = %s LIMIT 1",
            (discord_id,)
        )
        row = cur.fetchone()
        if row:
            return row["psn_name"]

        cur.execute(
            "SELECT psn_name FROM drivers WHERE discord_name = %s LIMIT 1",
            (discord_nick,)
        )
        row = cur.fetchone()
        return row["psn_name"] if row else None


def update_discord_id(discord_nick: str, discord_id: str) -> None:
    """Trägt die Discord-ID nach, wenn nur per Nickname gefunden."""
    with db_cursor() as cur:
        cur.execute(
            "UPDATE drivers SET discord_id = %s WHERE discord_name = %s AND (discord_id IS NULL OR discord_id = '')",
            (discord_id, discord_nick)
        )
        if cur.rowcount:
            logger.info(f"Discord-ID {discord_id} für {discord_nick} in DB eingetragen.")


def get_team_members_in_race(race_id: int, team_id: int, exclude_driver_id: int) -> list[dict]:
    """Gibt alle anderen Fahrer desselben Teams im Rennen zurück."""
    with db_cursor() as cur:
        cur.execute("""
            SELECT d.driver_id, d.psn_name
            FROM race_results rr
            JOIN drivers d ON d.driver_id = rr.driver_id
            WHERE rr.race_id = %s
              AND rr.team_id = %s
              AND rr.driver_id != %s
        """, (race_id, team_id, exclude_driver_id))
        return cur.fetchall()


def get_driver_team_in_race(race_id: int, driver_id: int) -> int | None:
    """Gibt die team_id des Fahrers in diesem Rennen zurück, oder None."""
    with db_cursor() as cur:
        cur.execute("""
            SELECT team_id FROM race_results
            WHERE race_id = %s AND driver_id = %s
            LIMIT 1
        """, (race_id, driver_id))
        row = cur.fetchone()
        return row["team_id"] if row else None


def get_driver_id(discord_id: str, discord_nick: str) -> int | None:
    """Gibt die driver_id zurück."""
    with db_cursor() as cur:
        cur.execute(
            "SELECT driver_id FROM drivers WHERE discord_id = %s LIMIT 1",
            (discord_id,)
        )
        row = cur.fetchone()
        if row:
            return row["driver_id"]
        cur.execute(
            "SELECT driver_id FROM drivers WHERE discord_name = %s LIMIT 1",
            (discord_nick,)
        )
        row = cur.fetchone()
        return row["driver_id"] if row else None


def get_grids_for_race(race_id: int) -> list[dict]:
    """Gibt alle Grids eines Rennens zurück."""
    with db_cursor() as cur:
        cur.execute("""
            SELECT grid_id, grid_number, grid_label
            FROM grids
            WHERE race_id = %s
            ORDER BY grid_number
        """, (race_id,))
        return cur.fetchall()


def get_grid_for_driver_id(race_id: int, driver_id: int) -> dict | None:
    """Gibt Grid (grid_id, grid_name) für eine bekannte driver_id zurück."""
    with db_cursor() as cur:
        cur.execute("""
            SELECT g.grid_id, g.grid_label AS grid_name
            FROM race_results rr
            JOIN grids g ON g.grid_id = rr.grid_id
            WHERE rr.race_id = %s
              AND rr.driver_id = %s
            LIMIT 1
        """, (race_id, driver_id))
        return cur.fetchone()
