"""
incident_bot.py – RTC_IncidentReportBot
Zeitgesteuerte Incident-Meldungen für die RTC Simracing Liga
"""

import os
import asyncio
import logging
import logging.handlers
from datetime import datetime, date, timedelta

import discord
from discord.ext import tasks
from dotenv import load_dotenv

import db
import sheets

load_dotenv()

# ─── Logging ──────────────────────────────────────────────────────────────────

logger = logging.getLogger("incident_bot")
logger.setLevel(logging.DEBUG)
fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

ch = logging.StreamHandler()
ch.setFormatter(fmt)
logger.addHandler(ch)

fh = logging.handlers.RotatingFileHandler(
    "incident_bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
)
fh.setFormatter(fmt)
logger.addHandler(fh)

# ─── Bot Setup ────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.members = True
bot = discord.Client(intents=intents)

# Globaler State
_current_race: dict | None = None
_report_open: bool = False
_results_found: bool = False


# ─── Hilfsfunktionen ──────────────────────────────────────────────────────────

def get_incident_channel() -> discord.TextChannel | None:
    chan_id = int(os.getenv("CHAN_INCIDENT", 0))
    return bot.get_channel(chan_id)


async def clear_channel(channel: discord.TextChannel):
    """Löscht alle Nachrichten im Channel."""
    try:
        await channel.purge(limit=None)
        logger.info(f"Channel #{channel.name} geleert.")
    except Exception as e:
        logger.error(f"Fehler beim Leeren des Channels: {e}")


def get_monday_date() -> date:
    now = datetime.now()
    weekday = now.weekday()
    if weekday == 0:
        return now.date()
    elif weekday == 1 and now.hour < 1:
        return (now - timedelta(days=1)).date()
    return None


def resolve_monday_for_window() -> date | None:
    """Gibt das Montags-Datum zurück wenn wir im Meldefenster sind, sonst None."""
    now = datetime.now()
    wd = now.weekday()
    if wd == 0 and now.hour >= 22:
        return now.date()
    elif wd == 1:
        return (now - timedelta(days=1)).date()
    elif wd == 2:
        return (now - timedelta(days=2)).date()
    elif wd == 3 and now.hour == 0:
        return (now - timedelta(days=3)).date()
    return None


# ─── Persistent Views ─────────────────────────────────────────────────────────

class ReportStartView(discord.ui.View):
    """View mit dem 'Melden'-Button im Channel."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="🚨 Vorfall melden",
        style=discord.ButtonStyle.danger,
        custom_id="incident:start"
    )
    async def start_report(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _current_race:
            await interaction.response.send_message(
                "Es ist aktuell kein Rennen aktiv.", ephemeral=True
            )
            return

        race_id = _current_race["race_id"]
        discord_id = str(interaction.user.id)
        discord_nick = interaction.user.display_name

        logger.info(f"Button geklickt: user={interaction.user}, discord_id={discord_id}, race_id={race_id}")

        grid = db.get_driver_grid_for_race(race_id, discord_id, discord_nick)
        if not grid:
            await interaction.response.send_message(
                "❌ Ich konnte dich keinem Grid zuordnen. "
                "Bitte stelle sicher, dass deine Discord-ID in der Datenbank hinterlegt ist.",
                ephemeral=True
            )
            return

        psn_name = db.get_psn_name(discord_id, discord_nick) or discord_nick
        drivers = db.get_drivers_in_grid(race_id, grid["grid_id"])
        other_drivers = [d for d in drivers if str(d.get("discord_id", "")) != discord_id]
        laps = _current_race.get("laps", 0)

        view = DriverSelectView(
            psn_name=psn_name,
            grid_name=grid["grid_name"],
            other_drivers=other_drivers,
            laps=laps,
            race=_current_race,
        )

        embed = build_embed_step1(psn_name, grid["grid_name"])
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ─── Mehrstufige Ephemeral-Flows ──────────────────────────────────────────────

def build_embed_step1(psn_name: str, grid_name: str) -> discord.Embed:
    embed = discord.Embed(title="🚨 Incident Meldung", color=discord.Color.red())
    embed.description = (
        f"Hallo **{psn_name}**, ich bedaure, dass es in Deinem Rennen einen Vorfall "
        f"gegeben hat, den Du melden möchtest. Du bist in **Grid {grid_name}** gestartet.\n\n"
        f"**Welchen Fahrer möchtest Du melden?**"
    )
    return embed


def build_embed_step2(psn_name: str, grid_name: str, reported_psn: str) -> discord.Embed:
    embed = discord.Embed(title="🚨 Incident Meldung", color=discord.Color.red())
    embed.description = (
        f"Hallo **{psn_name}**, ich bedaure, dass es in Deinem Rennen einen Vorfall "
        f"gegeben hat, den Du melden möchtest. Du bist in **Grid {grid_name}** gestartet.\n\n"
        f"Der am Incident beteiligte Fahrer ist **{reported_psn}**.\n\n"
        f"**In welcher Runde hat der Vorfall stattgefunden?**\n"
        f"Bitte gib eine Zahl ein und sende sie ab."
    )
    return embed


def build_embed_step3(psn_name: str, grid_name: str, reported_psn: str, lap: int) -> discord.Embed:
    embed = discord.Embed(title="🚨 Incident Meldung", color=discord.Color.red())
    embed.description = (
        f"Hallo **{psn_name}**, ich bedaure, dass es in Deinem Rennen einen Vorfall "
        f"gegeben hat, den Du melden möchtest. Du bist in **Grid {grid_name}** gestartet.\n\n"
        f"Der am Incident beteiligte Fahrer ist **{reported_psn}**.\n"
        f"Runde **{lap}**.\n\n"
        f"**Bitte beschreibe den Vorfall kurz mit Deinen eigenen Worten:**"
    )
    return embed


def build_embed_summary(
    psn_name: str, grid_name: str, reported_psn: str, lap: int, description: str
) -> discord.Embed:
    embed = discord.Embed(title="🚨 Incident Meldung – Zusammenfassung", color=discord.Color.orange())
    embed.description = (
        f"**Meldender:** {psn_name}\n"
        f"**Grid:** {grid_name}\n"
        f"**Gemeldeter Fahrer:** {reported_psn}\n"
        f"**Runde:** {lap}\n\n"
        f"**Schilderung:**\n{description}\n\n"
        f"*Möchtest Du diese Meldung an die Stewards weiterleiten? "
        f"Klicke auf **Abschicken**. "
        f"Du kannst diese Nachricht auch einfach schließen, wenn Du die Meldung doch nicht einreichen möchtest.*"
    )
    return embed


class DriverSelectView(discord.ui.View):
    def __init__(self, psn_name, grid_name, other_drivers, laps, race):
        super().__init__(timeout=300)
        self.psn_name = psn_name
        self.grid_name = grid_name
        self.other_drivers = other_drivers
        self.laps = laps
        self.race = race

        options = [
            discord.SelectOption(label=d["psn_name"], value=d["psn_name"])
            for d in other_drivers[:25]
        ]
        select = discord.ui.Select(
            placeholder="Fahrer auswählen …",
            options=options,
            min_values=1,
            max_values=1,
            custom_id="incident:driver_select"
        )
        select.callback = self.driver_selected
        self.add_item(select)

    async def driver_selected(self, interaction: discord.Interaction):
        reported_psn = interaction.data["values"][0]
        embed = build_embed_step2(self.psn_name, self.grid_name, reported_psn)
        view = LapInputView(
            psn_name=self.psn_name,
            grid_name=self.grid_name,
            reported_psn=reported_psn,
            laps=self.laps,
            race=self.race,
        )
        await interaction.response.edit_message(embed=embed, view=view)


class LapInputView(discord.ui.View):
    def __init__(self, psn_name, grid_name, reported_psn, laps, race):
        super().__init__(timeout=300)
        self.psn_name = psn_name
        self.grid_name = grid_name
        self.reported_psn = reported_psn
        self.laps = laps
        self.race = race

    @discord.ui.button(label="Runde eingeben", style=discord.ButtonStyle.primary)
    async def enter_lap(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = LapModal(
            psn_name=self.psn_name,
            grid_name=self.grid_name,
            reported_psn=self.reported_psn,
            laps=self.laps,
            race=self.race,
        )
        await interaction.response.send_modal(modal)


class LapModal(discord.ui.Modal, title="Runde des Vorfalls"):
    lap_input = discord.ui.TextInput(
        label="Runde",
        placeholder="z. B. 5",
        min_length=1,
        max_length=3,
    )

    def __init__(self, psn_name, grid_name, reported_psn, laps, race):
        super().__init__()
        self.psn_name = psn_name
        self.grid_name = grid_name
        self.reported_psn = reported_psn
        self.laps = laps
        self.race = race

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.lap_input.value.strip()

        if not raw.isdigit():
            error_embed = build_embed_step2(self.psn_name, self.grid_name, self.reported_psn)
            error_embed.set_footer(text=f"❌ '{raw}' ist keine gültige Zahl. Bitte erneut versuchen.")
            view = LapInputView(self.psn_name, self.grid_name, self.reported_psn, self.laps, self.race)
            await interaction.response.edit_message(embed=error_embed, view=view)
            return

        lap = int(raw)
        if not (1 <= lap <= self.laps):
            error_embed = build_embed_step2(self.psn_name, self.grid_name, self.reported_psn)
            error_embed.set_footer(
                text=f"❌ Runde {lap} existiert nicht (Rennen hat {self.laps} Runden). Bitte erneut versuchen."
            )
            view = LapInputView(self.psn_name, self.grid_name, self.reported_psn, self.laps, self.race)
            await interaction.response.edit_message(embed=error_embed, view=view)
            return

        embed = build_embed_step3(self.psn_name, self.grid_name, self.reported_psn, lap)
        view = DescriptionInputView(
            psn_name=self.psn_name,
            grid_name=self.grid_name,
            reported_psn=self.reported_psn,
            lap=lap,
            race=self.race,
        )
        await interaction.response.edit_message(embed=embed, view=view)


class DescriptionInputView(discord.ui.View):
    def __init__(self, psn_name, grid_name, reported_psn, lap, race):
        super().__init__(timeout=300)
        self.psn_name = psn_name
        self.grid_name = grid_name
        self.reported_psn = reported_psn
        self.lap = lap
        self.race = race

    @discord.ui.button(label="Vorfall beschreiben", style=discord.ButtonStyle.primary)
    async def enter_description(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = DescriptionModal(
            psn_name=self.psn_name,
            grid_name=self.grid_name,
            reported_psn=self.reported_psn,
            lap=self.lap,
            race=self.race,
        )
        await interaction.response.send_modal(modal)


class DescriptionModal(discord.ui.Modal, title="Schilderung des Vorfalls"):
    description_input = discord.ui.TextInput(
        label="Was ist passiert?",
        style=discord.TextStyle.paragraph,
        placeholder="Beschreibe den Vorfall mit Deinen eigenen Worten …",
        min_length=10,
        max_length=1000,
    )

    def __init__(self, psn_name, grid_name, reported_psn, lap, race):
        super().__init__()
        self.psn_name = psn_name
        self.grid_name = grid_name
        self.reported_psn = reported_psn
        self.lap = lap
        self.race = race

    async def on_submit(self, interaction: discord.Interaction):
        description = self.description_input.value.strip()
        embed = build_embed_summary(self.psn_name, self.grid_name, self.reported_psn, self.lap, description)
        view = ConfirmView(
            psn_name=self.psn_name,
            grid_name=self.grid_name,
            reported_psn=self.reported_psn,
            lap=self.lap,
            description=description,
            race=self.race,
        )
        await interaction.response.edit_message(embed=embed, view=view)


class ConfirmView(discord.ui.View):
    def __init__(self, psn_name, grid_name, reported_psn, lap, description, race):
        super().__init__(timeout=300)
        self.psn_name = psn_name
        self.grid_name = grid_name
        self.reported_psn = reported_psn
        self.lap = lap
        self.description = description
        self.race = race

    @discord.ui.button(label="✅ Abschicken", style=discord.ButtonStyle.success)
    async def submit(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Zuerst defer – gibt uns mehr Zeit für den Sheet-API-Call
        await interaction.response.defer()

        success = sheets.write_incident(
            race_number=self.race["race_number"],
            race_name=f"Race {self.race['race_number']}",
            track_name=self.race["track_name"],
            grid_name=self.grid_name,
            lap=self.lap,
            reporter_psn=self.psn_name,
            reported_psn=self.reported_psn,
            description=self.description,
        )

        if success:
            embed = discord.Embed(
                title="✅ Meldung eingereicht",
                description=(
                    f"Deine Meldung gegen **{self.reported_psn}** (Runde {self.lap}) "
                    f"wurde erfolgreich an die Stewards weitergeleitet.\n\n"
                    f"Danke für Deine Meldung."
                ),
                color=discord.Color.green()
            )
        else:
            embed = discord.Embed(
                title="❌ Fehler",
                description="Die Meldung konnte nicht gespeichert werden. Bitte versuche es erneut oder kontaktiere einen Admin.",
                color=discord.Color.red()
            )

        await interaction.edit_original_response(embed=embed, view=None)


# ─── Zeitgesteuerte Tasks ─────────────────────────────────────────────────────

@tasks.loop(minutes=5)
async def check_results_loop():
    global _current_race, _report_open, _results_found

    if _report_open:
        return

    now = datetime.now()
    monday_date = get_monday_date()

    if monday_date is None:
        return

    race = db.get_race_for_date(monday_date)
    if not race:
        logger.debug(f"Kein (wertbares) Rennen am {monday_date}, überspringe.")
        check_results_loop.stop()
        return

    _current_race = race

    if now.weekday() == 1 and now.hour >= 1:
        logger.warning("01:00 Uhr erreicht, Meldefenster wird trotzdem geöffnet.")
        await open_report_window(results_found=False)
        check_results_loop.stop()
        return

    try:
        grid_count = sheets.get_grid_count()
        complete = sheets.check_results_complete(race["race_number"], grid_count)
    except Exception as e:
        logger.error(f"Fehler beim Ergebnis-Check: {e}")
        return

    if complete:
        _results_found = True
        await open_report_window(results_found=True)
        check_results_loop.stop()


@tasks.loop(minutes=1)
async def scheduler_monday():
    now = datetime.now()
    if now.weekday() == 0 and now.hour == 22 and now.minute == 0:
        logger.info("Montag 22:00 – starte Ergebnis-Check-Loop.")
        if not check_results_loop.is_running():
            check_results_loop.start()


@tasks.loop(minutes=1)
async def scheduler_thursday():
    global _report_open, _current_race
    now = datetime.now()
    if now.weekday() == 3 and now.hour == 0 and now.minute == 0:
        if not _report_open:
            return
        logger.info("Donnerstag 00:00 – schließe Meldefenster.")
        await close_report_window()


async def open_report_window(results_found: bool):
    global _report_open

    channel = get_incident_channel()
    if not channel:
        logger.error("CHAN_INCIDENT nicht gefunden!")
        return

    await clear_channel(channel)
    _report_open = True

    race = _current_race
    hint = "" if results_found else "\n*(Hinweis: Die Ergebnisse konnten nicht vollständig verifiziert werden.)*"

    embed = discord.Embed(
        title="🏁 Incident-Meldung geöffnet",
        description=(
            f"Die Meldung von Vorfällen aus **Rennen {race['race_number']} – {race['track_name']}** "
            f"ist jetzt möglich.{hint}\n\n"
            f"Um den Meldevorgang zu starten, klicke auf den Button."
        ),
        color=discord.Color.red()
    )

    view = ReportStartView()
    await channel.send(embed=embed, view=view)
    logger.info(f"Meldefenster für Rennen {race['race_number']} geöffnet.")


async def close_report_window():
    global _report_open

    channel = get_incident_channel()
    if not channel:
        return

    await clear_channel(channel)
    _report_open = False

    race = _current_race
    embed = discord.Embed(
        title="🔒 Meldefrist abgelaufen",
        description=(
            f"Die Meldefrist für **Rennen {race['race_number']} – {race['track_name']}** "
            f"ist abgelaufen. Es können keine Incidents mehr gemeldet werden."
        ),
        color=discord.Color.dark_grey()
    )
    await channel.send(embed=embed)
    logger.info(f"Meldefenster für Rennen {race['race_number']} geschlossen.")


# ─── Startup Check ────────────────────────────────────────────────────────────

async def get_last_bot_message(channel: discord.TextChannel) -> discord.Message | None:
    async for msg in channel.history(limit=10):
        if msg.author == bot.user:
            return msg
    return None


async def startup_check():
    global _current_race, _report_open

    await asyncio.sleep(2)

    channel = get_incident_channel()
    if not channel:
        logger.error("Startup-Check: CHAN_INCIDENT nicht gefunden!")
        return

    monday_date = resolve_monday_for_window()
    in_window = monday_date is not None

    logger.info(f"Startup-Check: in_window={in_window}, monday_date={monday_date}")

    if not in_window:
        logger.info("Startup-Check: Außerhalb des Meldefensters, nichts zu tun.")
        return

    race = db.get_race_for_date(monday_date)
    if not race:
        logger.info(f"Startup-Check: Kein (wertbares) Rennen am {monday_date}.")
        return

    _current_race = race
    logger.info(f"Startup-Check: Rennen gefunden – {race['race_number']} auf {race['track_name']}")

    last_msg = await get_last_bot_message(channel)

    if last_msg and last_msg.embeds:
        title = last_msg.embeds[0].title or ""
        if "Incident-Meldung geöffnet" in title:
            logger.info("Startup-Check: Startmeldung bereits im Channel, setze _report_open=True.")
            _report_open = True
            return
        if "Meldefrist abgelaufen" in title:
            logger.info("Startup-Check: Abschlussmeldung bereits im Channel, nichts zu tun.")
            return

    logger.info("Startup-Check: Keine passende Nachricht im Channel, prüfe Ergebnisse.")
    try:
        grid_count = sheets.get_grid_count()
        complete = sheets.check_results_complete(race["race_number"], grid_count)
    except Exception as e:
        logger.error(f"Startup-Check: Fehler beim Ergebnis-Check: {e}")
        complete = False

    await open_report_window(results_found=complete)

    if not complete and not check_results_loop.is_running():
        logger.info("Startup-Check: Ergebnisse unvollständig, starte Check-Loop.")
        check_results_loop.start()


# ─── Bot Events ───────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    global _current_race
    logger.info(f"Bot eingeloggt als {bot.user} (ID: {bot.user.id})")

    # Persistent View registrieren
    bot.add_view(ReportStartView())

    # Scheduler starten
    if not scheduler_monday.is_running():
        scheduler_monday.start()
    if not scheduler_thursday.is_running():
        scheduler_thursday.start()

    logger.info("Scheduler gestartet.")

    # _current_race sofort setzen damit Button-Handler nicht an None scheitert
    monday_date = resolve_monday_for_window()
    if monday_date:
        try:
            race = db.get_race_for_date(monday_date)
            if race:
                _current_race = race
                logger.info(f"on_ready: _current_race = Rennen {race['race_number']} auf {race['track_name']}")
        except Exception as e:
            logger.error(f"on_ready: Fehler beim Laden des Rennens: {e}")

    # Startup-Check asynchron starten
    bot.loop.create_task(startup_check())


# ─── Start ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN nicht gesetzt!")
    bot.run(token)
