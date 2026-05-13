"""
sheets.py – Google Sheets Zugriff für den RTC_IncidentReportBot

Ergebnis-Sheet: Lesezugriff (Anzahl Grids + Ergebnisse)
Incident-Sheet: Schreibzugriff (Meldungen eintragen)
"""

import os
import logging
import string
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_service = None


def _get_service():
    global _service
    if _service is None:
        creds = Credentials.from_service_account_file(
            os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json"),
            scopes=SCOPES,
        )
        _service = build("sheets", "v4", credentials=creds)
    return _service


def _col_letter(n: int) -> str:
    """Konvertiert einen 0-basierten Spaltenindex in einen Buchstaben (A, B, ..., Z, AA, ...)."""
    result = ""
    n += 1
    while n:
        n, r = divmod(n - 1, 26)
        result = string.ascii_uppercase[r] + result
    return result


# ─── Ergebnis-Sheet (Lesezugriff) ─────────────────────────────────────────────

# Spalten für Platz-1-Check je Rennen (0-basierter Index, A=0)
# Rennen 1 → D (3), Rennen 2 → R (17), Rennen 3 → AF (31), ...
# Muster: D=3, dann +14 pro Rennen
def _result_col_index(race_number: int) -> int:
    return 3 + (race_number - 1) * 14


# Zeile für Grid (1-basiert im Sheet, 0-basiert intern)
# Grid 1 → Zeile 5, Grid 2 → Zeile 25, ...
def _result_row(grid_index: int) -> int:
    """grid_index ist 0-basiert (Grid 1 = 0)."""
    return 5 + grid_index * 20


def get_grid_count() -> int:
    """Liest die Anzahl der Grids aus Apollo-Grabber!I1."""
    sheet_id = os.getenv("RESULTS_SHEET_ID")
    tab = os.getenv("APOLLO_GRABBER_TAB", "Apollo-Grabber")
    svc = _get_service()
    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"'{tab}'!I1"
    ).execute()
    values = result.get("values", [])
    if not values or not values[0]:
        logger.warning("Keine Grid-Anzahl in I1 gefunden, nehme 1 an.")
        return 1
    return int(values[0][0])


def check_results_complete(race_number: int, grid_count: int) -> bool:
    """
    Prüft ob für alle Grids der erste Platz in Blatt T eingetragen ist.
    Gibt True zurück wenn alle Ergebnisse vorliegen.
    """
    sheet_id = os.getenv("RESULTS_SHEET_ID")
    tab = os.getenv("RESULTS_SHEET_TAB", "T")
    svc = _get_service()
    col_letter = _col_letter(_result_col_index(race_number))

    for grid_idx in range(grid_count):
        row = _result_row(grid_idx)
        range_str = f"'{tab}'!{col_letter}{row}"
        result = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=range_str
        ).execute()
        values = result.get("values", [])
        if not values or not values[0] or not str(values[0][0]).strip():
            logger.debug(f"Ergebnis fehlt: Grid {grid_idx + 1}, Spalte {col_letter}, Zeile {row}")
            return False

    logger.info(f"Alle {grid_count} Grid-Ergebnisse für Rennen {race_number} gefunden.")
    return True


# ─── Incident-Sheet (Schreibzugriff) ──────────────────────────────────────────

def get_next_incident_row(race_number: int) -> int:
    """Gibt die nächste freie Zeile im Incident-Sheet zurück (ab Zeile 5)."""
    sheet_id = os.getenv("INCIDENT_SHEET_ID")
    tab = str(race_number)
    svc = _get_service()
    result = svc.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range=f"'{tab}'!B:B"
    ).execute()
    values = result.get("values", [])
    # Zeilen 1-4 sind Header, ab Zeile 5 Daten
    filled = len([v for v in values if v and str(v[0]).strip()])
    # Mindestens ab Zeile 5
    return max(5, filled + 1)


def get_incident_count(race_number: int) -> int:
    """Gibt die Anzahl bereits eingetragener Incidents zurück."""
    return get_next_incident_row(race_number) - 5


def write_incident(
    race_number: int,
    race_name: str,
    track_name: str,
    grid_name: str,
    lap: int,
    reporter_psn: str,
    reported_psn: str,
    description: str,
) -> bool:
    """
    Schreibt einen Incident ins Sheet (Blatt 'Formularantworten').
    Spalten: A=Zeitstempel, B=leer, C=PSN Meldender, D=RaceNumber,
             E=GridNumber, F=Lap, G=Opponent, H=Beschreibung
    Wenn DISABLE_SHEET_WRITE=true, wird nur geloggt.
    """
    if os.getenv("DISABLE_SHEET_WRITE", "false").lower() == "true":
        logger.info(
            f"[TESTMODUS] Sheet-Eintrag unterdrückt: "
            f"Rennen {race_number} | Grid {grid_name} | Runde {lap} | "
            f"{reporter_psn} meldet {reported_psn} | {description!r}"
        )
        return True

    try:
        sheet_id = os.getenv("INCIDENT_SHEET_ID")
        tab = "Formularantworten"
        svc = _get_service()

        # Nächste freie Zeile ermitteln (ab Zeile 2, Zeile 1 = Header)
        result = svc.spreadsheets().values().get(
            spreadsheetId=sheet_id,
            range=f"'{tab}'!A:A"
        ).execute()
        values_col = result.get("values", [])
        next_row = max(2, len(values_col) + 1)

        from datetime import datetime as dt
        timestamp = dt.now().strftime("%d.%m.%Y %H:%M:%S")

        values = [[
            timestamp,      # A – Zeitstempel
            "",             # B – leer
            reporter_psn,   # C – PSN Meldender
            race_number,    # D – RaceNumber
            grid_name,      # E – GridNumber
            str(lap),       # F – Lap
            reported_psn,   # G – Opponent
            description,    # H – Beschreibung
        ]]

        svc.spreadsheets().values().update(
            spreadsheetId=sheet_id,
            range=f"'{tab}'!A{next_row}",
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()

        logger.info(f"Incident in Sheet eingetragen (Zeile {next_row}): {reporter_psn} meldet {reported_psn}, Rennen {race_number}, Grid {grid_name}, Runde {lap}.")
        return True

    except Exception as e:
        logger.error(f"Fehler beim Sheet-Eintrag: {e}")
        return False
