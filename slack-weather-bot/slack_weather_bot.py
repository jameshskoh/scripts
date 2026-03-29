"""
weather_bot.py — Slack bot that fetches current weather for a city and posts
a Claude-generated summary to a Slack channel.

Architecture:
  - Socket Mode (no public URL needed — works on a local machine)
  - APScheduler BackgroundScheduler runs the periodic job in a background thread
  - Slack Bolt handles the slash command in the same process
  - All secrets are loaded from a .env file via python-dotenv
  - Weather data from Open-Meteo (free, no API key required)
  - Human-readable summary generated via `claude -p` subprocess

Setup:
  1. cp .env.example .env  →  fill in your real values (see env vars below)
  2. Run env-setup.sh to create the virtualenv and install dependencies
  3. Run env-start.sh to activate the virtualenv and start the bot

Environment variables (all read from .env):
  Required:
    SLACK_BOT_TOKEN      — xoxb-... bot token
    SLACK_APP_TOKEN      — xapp-... app-level token (Socket Mode)
    SLACK_CHANNEL_ID     — channel to post weather to (e.g. C01234567)

  Optional (defaults shown):
    SLACK_SLASH_COMMAND  — slash command name (default: /get-weather)
    DEFAULT_CITY         — city for scheduled posts (default: London)
    SCHEDULE_CRON        — 5-field cron expression for periodic posting,
                           e.g. "0 9 * * 1-5" (default: unset = disabled)
    SCHEDULE_TIMEZONE    — timezone for SCHEDULE_CRON (default: UTC)
    LLM_PROMPT           — override the system prompt sent to claude CLI

Slack App settings required (api.slack.com/apps):
  - Socket Mode: ENABLED
  - App-Level Token scope: connections:write
  - Bot Token Scopes: chat:write
  - Slash Commands: create /get-weather (default), no Request URL needed for Socket Mode
"""

import json
import logging
import os
import subprocess
import time

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("weather_bot")

# ---------------------------------------------------------------------------
# Load configuration from .env
# ---------------------------------------------------------------------------
load_dotenv()


def _require_env(key: str) -> str:
    """Read an env var, raising a clear error if it is missing or empty."""
    value = os.getenv(key, "").strip()
    if not value:
        raise EnvironmentError(
            f"Required environment variable '{key}' is not set. "
            f"Check your .env file."
        )
    return value


# Slack
SLACK_BOT_TOKEN = _require_env("SLACK_BOT_TOKEN")
SLACK_APP_TOKEN = _require_env("SLACK_APP_TOKEN")
SLACK_CHANNEL_ID = _require_env("SLACK_CHANNEL_ID")
SLACK_SLASH_COMMAND = os.getenv("SLACK_SLASH_COMMAND", "/get-weather").strip()

# Weather / LLM
DEFAULT_CITY = os.getenv("DEFAULT_CITY", "London").strip()
LLM_PROMPT = os.getenv("LLM_PROMPT", "").strip() or """\
You are a weather briefing assistant. Given structured JSON weather data, produce \
a concise daily briefing formatted for Slack (use *bold*, plain text, and emoji — \
no markdown tables, as Slack does not render them).

Use this exact structure:

*Weather Briefing — <City, Country>*

*Overview*
One sentence on overall conditions. Then 1–3 short actionable lines, e.g.:
  ☂️ Rain expected — bring an umbrella.
  🧴 UV is high — wear sunscreen.
  🥵 Feels very hot — stay hydrated.
Only include lines that are relevant given today's data.

*Temperature*
A monospaced ASCII table wrapped in a ```code block```. Exactly 25 characters wide per row \
(pad with spaces as needed). Four rows for 6am, 12pm, 3pm, 9pm. Columns:

Time | Temp/Feels°C | Hum%
Use format: time ≤4 chars (6am, 12pm, 3pm, 9pm). Temp/Feels as two whole numbers hi/lo \
(e.g. 28/25). Humidity as whole number with % (e.g. 72%). No °C unit in cells to save space.

Example layout (use this exact column order and spacing):
```
Time |Tmp/Fel|Hum
6am  |28/25  |72%
12pm |32/30  |65%
3pm  |31/29  |68%
9pm  |26/24  |80%
Hi/Lo: 33/24°C
```
After the code block, add one plain line: Hi/Lo: <max>/<min>°C (whole numbers, from daily data).

*Rain*
Start with one sentence on what to expect for the whole day. Then for each hourly_snapshot \
period that has precipitation_probability > 20%, list it as:
  • <time>: <probability>% — <rating>
Rate intensity as: light / moderate / heavy. Base rating on weathercode:
  51/61/80 = light, 53/63/81 = moderate, 55/65/82 = heavy.
  If weathercode is clear/cloudy (not rain), use probability alone: <30% = light, \
  30–60% = moderate, >60% = heavy.
If no period exceeds 20%, write: No significant rain expected.

*Sun & UV*
- UV index peak: <uv_index_max> — classify as Low (0–2), Moderate (3–5), High (6–7), \
  Very High (8–10), Extreme (11+)
- Sunrise: <time> / Sunset: <time>

Be precise and factual. Do not add conversational filler. Output only the briefing, no preamble.\
"""

# Scheduler
SCHEDULE_CRON = os.getenv("SCHEDULE_CRON", "").strip()
SCHEDULE_TIMEZONE = os.getenv("SCHEDULE_TIMEZONE", "UTC").strip()

# ---------------------------------------------------------------------------
# Open-Meteo weather helpers
# ---------------------------------------------------------------------------

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


def geocode_city(city: str) -> dict:
    """
    Resolve a city name to geographic coordinates via the Open-Meteo geocoding API.

    Returns a dict with keys: latitude, longitude, name, country.
    Raises ValueError if the city is not found.
    """
    logger.info("Geocoding city: %s", city)
    response = requests.get(
        GEOCODING_URL,
        params={"name": city, "count": 1},
        timeout=15,
    )
    response.raise_for_status()
    data = response.json()

    results = data.get("results")
    if not results:
        raise ValueError(f"City not found: '{city}'")

    result = results[0]
    return {
        "latitude": result["latitude"],
        "longitude": result["longitude"],
        "name": result["name"],
        "country": result.get("country", ""),
    }


def fetch_weather(latitude: float, longitude: float) -> dict:
    """
    Fetch current, hourly, and daily weather from the Open-Meteo forecast API.

    Hourly data is sliced to four representative times: 6am, 12pm, 3pm, 9pm.
    Returns a structured dict with current, hourly_snapshot, and daily keys.
    """
    logger.info("Fetching weather for lat=%s lon=%s", latitude, longitude)
    response = requests.get(
        FORECAST_URL,
        params={
            "latitude": latitude,
            "longitude": longitude,
            "current": ",".join([
                "temperature_2m",
                "weathercode",
                "wind_speed_10m",
                "precipitation",
                "relative_humidity_2m",
                "apparent_temperature",
            ]),
            "hourly": ",".join([
                "temperature_2m",
                "apparent_temperature",
                "relative_humidity_2m",
                "precipitation_probability",
                "precipitation",
                "weathercode",
                "uv_index",
            ]),
            "daily": ",".join([
                "temperature_2m_max",
                "temperature_2m_min",
                "apparent_temperature_max",
                "apparent_temperature_min",
                "precipitation_sum",
                "precipitation_probability_max",
                "weathercode",
                "uv_index_max",
                "sunrise",
                "sunset",
            ]),
            "wind_speed_unit": "kmh",
            "timezone": "auto",
            "forecast_days": 1,
        },
        timeout=15,
    )
    response.raise_for_status()
    raw = response.json()

    # Slice hourly arrays to the four snapshot hours for today only.
    # The API returns 24 values (indices 0–23) for the first day.
    SNAPSHOT_HOURS = {"morning": 6, "noon": 12, "afternoon": 15, "night": 21}
    hourly = raw.get("hourly", {})
    hourly_snapshot = {}
    for label, hour in SNAPSHOT_HOURS.items():
        hourly_snapshot[label] = {
            "time": f"{hour:02d}:00",
            "temperature_2m": hourly.get("temperature_2m", [None] * 24)[hour],
            "apparent_temperature": hourly.get("apparent_temperature", [None] * 24)[hour],
            "relative_humidity_2m": hourly.get("relative_humidity_2m", [None] * 24)[hour],
            "precipitation_probability": hourly.get("precipitation_probability", [None] * 24)[hour],
            "precipitation": hourly.get("precipitation", [None] * 24)[hour],
            "weathercode": hourly.get("weathercode", [None] * 24)[hour],
            "uv_index": hourly.get("uv_index", [None] * 24)[hour],
        }

    # Daily values are lists with one entry (forecast_days=1); unwrap to scalars.
    daily_raw = raw.get("daily", {})
    daily = {k: (v[0] if isinstance(v, list) and v else v) for k, v in daily_raw.items()}

    return {
        "current": raw.get("current", {}),
        "current_units": raw.get("current_units", {}),
        "hourly_snapshot": hourly_snapshot,
        "hourly_units": raw.get("hourly_units", {}),
        "daily": daily,
        "daily_units": raw.get("daily_units", {}),
    }


def get_weather_data(city: str) -> dict:
    """
    Geocode city and fetch its current weather, returning a combined dict.
    """
    location = geocode_city(city)
    weather = fetch_weather(location["latitude"], location["longitude"])
    return {
        "location": location,
        "weather": weather,
    }


# ---------------------------------------------------------------------------
# LLM summarisation via `claude -p`
# ---------------------------------------------------------------------------

def summarise_weather(weather_data: dict) -> str:
    """
    Pass weather JSON to the claude CLI and return the generated summary.

    Raises RuntimeError if the subprocess exits with a non-zero return code.
    """
    prompt = LLM_PROMPT + "\n\n" + json.dumps(weather_data, indent=2)
    logger.info("Calling claude CLI to summarise weather data")
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or "claude CLI exited with non-zero status")
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# Core action
# ---------------------------------------------------------------------------

# Initialise the Bolt app with the bot token only.
# The signing secret is NOT required when using Socket Mode.
app = App(token=SLACK_BOT_TOKEN)


def post_weather_to_slack(
    city: str,
    triggered_by: str = "scheduler",
    slack_client=None,
) -> None:
    """
    Fetch weather for city, summarise with Claude, and post to Slack.

    Args:
        city:         City name to look up.
        triggered_by: Label for audit trail in logs (user or "scheduler").
        slack_client: Optional override for the Slack WebClient.
    """
    client = slack_client or app.client

    try:
        weather_data = get_weather_data(city)
    except ValueError as exc:
        error_msg = f":x: Could not find city: `{exc}`"
        logger.error(error_msg)
        client.chat_postMessage(channel=SLACK_CHANNEL_ID, text=error_msg)
        return
    except requests.HTTPError as exc:
        error_msg = f":x: Weather API error: `{exc}`"
        logger.error(error_msg)
        client.chat_postMessage(channel=SLACK_CHANNEL_ID, text=error_msg)
        return
    except Exception as exc:
        logger.exception("Unexpected error fetching weather")
        client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            text=f":x: Unexpected error fetching weather: `{exc}`",
        )
        return

    try:
        summary = summarise_weather(weather_data)
    except Exception as first_exc:
        logger.warning("LLM summarisation failed on first attempt: %s — retrying in 5s", first_exc)
        client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            text=":warning: Failed to generate weather summary, retrying once in 5 seconds...",
        )
        time.sleep(5)
        try:
            summary = summarise_weather(weather_data)
        except (RuntimeError, FileNotFoundError) as exc:
            error_msg = f":x: Failed to generate weather summary: `{exc}`"
            logger.error(error_msg)
            client.chat_postMessage(channel=SLACK_CHANNEL_ID, text=error_msg)
            return
        except Exception as exc:
            logger.exception("Unexpected error during LLM summarisation (retry)")
            client.chat_postMessage(
                channel=SLACK_CHANNEL_ID,
                text=f":x: Unexpected error generating summary: `{exc}`",
            )
            return

    logger.info("Posting weather summary to channel %s (triggered by: %s)", SLACK_CHANNEL_ID, triggered_by)
    client.chat_postMessage(channel=SLACK_CHANNEL_ID, text=summary)


# ---------------------------------------------------------------------------
# Slash command handler
# ---------------------------------------------------------------------------

@app.command(SLACK_SLASH_COMMAND)
def handle_slash_command(ack, respond, command, client):
    """
    Respond to the slash command immediately (Slack requires an ack within
    3 seconds), then fetch weather and post the summary.
    """
    ack()

    user_id = command.get("user_id", "unknown")
    user_name = command.get("user_name", "someone")
    city = command.get("text", "").strip() or DEFAULT_CITY

    logger.info(
        "Slash command '%s' triggered by %s (%s) for city: %s",
        SLACK_SLASH_COMMAND,
        user_name,
        user_id,
        city,
    )

    respond(
        text=f":partly_sunny: Fetching weather for *{city}*...",
        response_type="ephemeral",
    )

    post_weather_to_slack(
        city=city,
        triggered_by=f"<@{user_id}> via `{SLACK_SLASH_COMMAND}`",
        slack_client=client,
    )


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

def _parse_cron(cron_str: str) -> dict:
    """
    Parse a 5-field cron string (minute hour day month day_of_week)
    into a kwargs dict for APScheduler's CronTrigger.
    """
    fields = cron_str.split()
    if len(fields) != 5:
        raise ValueError(
            f"SCHEDULE_CRON must be a 5-field cron expression "
            f"(e.g. '0 9 * * *'), got: '{cron_str}'"
        )
    minute, hour, day, month, day_of_week = fields
    return dict(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
    )


def start_scheduler() -> BackgroundScheduler | None:
    """
    Start the APScheduler BackgroundScheduler if SCHEDULE_CRON is configured.
    Returns the scheduler instance, or None if scheduling is disabled.
    """
    if not SCHEDULE_CRON:
        logger.info(
            "SCHEDULE_CRON is not set — periodic posting is disabled. "
            "Bot will only respond to slash commands."
        )
        return None

    try:
        cron_kwargs = _parse_cron(SCHEDULE_CRON)
    except ValueError as exc:
        logger.error("Invalid SCHEDULE_CRON: %s — scheduler not started.", exc)
        return None

    scheduler = BackgroundScheduler(timezone=SCHEDULE_TIMEZONE)
    scheduler.add_job(
        func=post_weather_to_slack,
        trigger="cron",
        kwargs={"city": DEFAULT_CITY, "triggered_by": "scheduler (automatic)"},
        **cron_kwargs,
        id="weather_job",
        name="Fetch and post weather summary",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "Scheduler started. Cron: '%s' | Timezone: %s | City: %s",
        SCHEDULE_CRON,
        SCHEDULE_TIMEZONE,
        DEFAULT_CITY,
    )
    return scheduler


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("Starting weather bot")
    logger.info("Slash command: %s", SLACK_SLASH_COMMAND)
    logger.info("Default city: %s", DEFAULT_CITY)
    logger.info("Target Slack channel: %s", SLACK_CHANNEL_ID)

    scheduler = start_scheduler()

    try:
        logger.info("Connecting to Slack via Socket Mode...")
        handler = SocketModeHandler(app, SLACK_APP_TOKEN)
        handler.start()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        if scheduler and scheduler.running:
            scheduler.shutdown(wait=False)
            logger.info("Scheduler shut down.")
