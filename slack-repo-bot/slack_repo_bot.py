"""
repo_bot.py — Slack bot that periodically (or on-demand) downloads a GitHub
repository as a ZIP file and posts it to a Slack channel.

Architecture:
  - Socket Mode (no public URL needed — works on a local machine)
  - APScheduler BackgroundScheduler runs the periodic job in a background thread
  - Slack Bolt handles the slash command in the same process
  - All secrets are loaded from a .env file via python-dotenv

Setup:
  1. cp .env.example .env  →  fill in your real values (see env vars below)
  2. Run env-setup.sh to create the virtualenv and install dependencies
  3. Run env-start.sh to activate the virtualenv and start the bot

Environment variables (all read from .env):
  Required:
    SLACK_BOT_TOKEN      — xoxb-... bot token
    SLACK_APP_TOKEN      — xapp-... app-level token (Socket Mode)
    SLACK_CHANNEL_ID     — channel to post ZIPs to (e.g. C01234567)
    GITHUB_TOKEN         — personal access token with repo read access
    GITHUB_REPO_OWNER    — GitHub user or org name
    GITHUB_REPO_NAME     — repository name

  Optional (defaults shown):
    SLACK_SLASH_COMMAND  — slash command name (default: /get-repo)
    GITHUB_BRANCH        — branch to archive (default: main)
    SCHEDULE_CRON        — 5-field cron expression for periodic posting,
                           e.g. "0 9 * * 1-5" (default: unset = disabled)
    SCHEDULE_TIMEZONE    — timezone for SCHEDULE_CRON (default: UTC)

Slack App settings required (api.slack.com/apps):
  - Socket Mode: ENABLED
  - App-Level Token scope: connections:write
  - Bot Token Scopes: chat:write, files:write
  - Slash Commands: create /get-repo (default), no Request URL needed for Socket Mode
  - Event Subscriptions: not required for slash commands only
"""

import logging
import os
import tempfile
from datetime import datetime

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
logger = logging.getLogger("repo_bot")

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
SLACK_SLASH_COMMAND = os.getenv("SLACK_SLASH_COMMAND", "/get-repo").strip()

# GitHub
GITHUB_TOKEN = _require_env("GITHUB_TOKEN")
GITHUB_REPO_OWNER = _require_env("GITHUB_REPO_OWNER")
GITHUB_REPO_NAME = _require_env("GITHUB_REPO_NAME")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main").strip()

# Scheduler
SCHEDULE_CRON = os.getenv("SCHEDULE_CRON", "").strip()
SCHEDULE_TIMEZONE = os.getenv("SCHEDULE_TIMEZONE", "UTC").strip()

# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

GITHUB_API_BASE = "https://api.github.com"


def _github_headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        # GitHub requires a User-Agent header
        "User-Agent": "repo-slack-bot/1.0",
    }


def download_repo_zip() -> tuple[str, str]:
    """
    Download the repository archive as a ZIP file to a temporary location.

    Returns:
        (local_file_path, zip_filename)  — caller is responsible for cleanup.

    Raises:
        requests.HTTPError: on non-2xx responses from the GitHub API.
    """
    url = (
        f"{GITHUB_API_BASE}/repos/{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}"
        f"/zipball/{GITHUB_BRANCH}"
    )
    logger.info("Downloading ZIP from: %s", url)

    # The GitHub API returns a 302 redirect to the actual file; requests follows
    # it automatically when allow_redirects=True (the default).
    response = requests.get(
        url,
        headers=_github_headers(),
        stream=True,
        timeout=60,
    )
    response.raise_for_status()

    # Build a human-readable filename: owner-repo-branch-YYYYMMDD.zip
    date_stamp = datetime.now().strftime("%Y%m%d")
    zip_filename = f"{GITHUB_REPO_OWNER}-{GITHUB_REPO_NAME}-{GITHUB_BRANCH}-{date_stamp}.zip"

    # Write to a temporary file so we never leave stale zips on disk
    tmp = tempfile.NamedTemporaryFile(
        suffix=".zip",
        prefix="repo_bot_",
        delete=False,  # We delete it manually after uploading
    )
    try:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                tmp.write(chunk)
        tmp.flush()
    finally:
        tmp.close()

    logger.info("Saved ZIP to temp file: %s (%s)", tmp.name, zip_filename)
    return tmp.name, zip_filename


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

# Initialise the Bolt app with the bot token only.
# The signing secret is NOT required when using Socket Mode.
app = App(token=SLACK_BOT_TOKEN)


def post_repo_zip_to_slack(
    triggered_by: str = "scheduler",
    slack_client=None,
) -> None:
    """
    Download the repo ZIP and upload it to the configured Slack channel.

    Args:
        triggered_by: A short label shown in the Slack message ("scheduler"
                      or the user ID who ran the slash command).
        slack_client:  An optional override for the Slack WebClient (useful
                       inside Bolt listeners where `client` is injected).
    """
    client = slack_client or app.client

    try:
        local_path, zip_filename = download_repo_zip()
    except requests.HTTPError as exc:
        error_msg = (
            f":x: Failed to download `{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}` "
            f"from GitHub.\nError: `{exc}`"
        )
        logger.error(error_msg)
        client.chat_postMessage(channel=SLACK_CHANNEL_ID, text=error_msg)
        return
    except Exception as exc:
        logger.exception("Unexpected error during ZIP download")
        client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            text=f":x: Unexpected error while downloading the repo ZIP: `{exc}`",
        )
        return

    try:
        # files_upload_v2 is the current recommended method (files.upload is
        # deprecated as of May 2024).
        logger.info("Uploading %s to Slack channel %s", zip_filename, SLACK_CHANNEL_ID)
        client.files_upload_v2(
            channel=SLACK_CHANNEL_ID,
            file=local_path,
            filename=zip_filename,
            title=f"{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME} @ {GITHUB_BRANCH}",
            initial_comment=(
                f":package: Here is the latest snapshot of "
                f"`{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}` "
                f"(branch: `{GITHUB_BRANCH}`).\n"
                f"_Triggered by: {triggered_by}_"
            ),
        )
        logger.info("Upload complete.")
    except Exception as exc:
        logger.exception("Failed to upload ZIP to Slack")
        client.chat_postMessage(
            channel=SLACK_CHANNEL_ID,
            text=f":x: Failed to upload ZIP to Slack: `{exc}`",
        )
    finally:
        # Always clean up the temp file
        if os.path.exists(local_path):
            os.remove(local_path)
            logger.info("Cleaned up temp file: %s", local_path)


# ---------------------------------------------------------------------------
# Slash command handler
# ---------------------------------------------------------------------------

@app.command(SLACK_SLASH_COMMAND)
def handle_slash_command(ack, respond, command, client):
    """
    Respond to the slash command immediately (Slack requires an ack within
    3 seconds), then do the actual work in the same thread.

    Because files_upload_v2 can take a few seconds for large repos, we first
    send an ephemeral acknowledgement, then do the upload.
    """
    # 1. Acknowledge immediately to prevent Slack's "operation_timeout" error.
    ack()

    user_id = command.get("user_id", "unknown")
    user_name = command.get("user_name", "someone")
    logger.info("Slash command '%s' triggered by %s (%s)", SLACK_SLASH_COMMAND, user_name, user_id)

    # 2. Let the user know we heard them (ephemeral = only visible to them).
    respond(
        text=(
            f":hourglass: Got it! Downloading the latest ZIP of "
            f"`{GITHUB_REPO_OWNER}/{GITHUB_REPO_NAME}` "
            f"(branch: `{GITHUB_BRANCH}`) and posting to the channel..."
        ),
        response_type="ephemeral",
    )

    # 3. Do the actual work — using the injected client so the upload is
    #    attributed to the bot correctly.
    post_repo_zip_to_slack(
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
        func=post_repo_zip_to_slack,
        trigger="cron",
        kwargs={"triggered_by": "scheduler (automatic)"},
        **cron_kwargs,
        id="repo_zip_job",
        name="Download and post repo ZIP",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "Scheduler started. Cron: '%s' | Timezone: %s",
        SCHEDULE_CRON,
        SCHEDULE_TIMEZONE,
    )
    return scheduler


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info(
        "Starting repo bot for %s/%s (branch: %s)",
        GITHUB_REPO_OWNER,
        GITHUB_REPO_NAME,
        GITHUB_BRANCH,
    )
    logger.info("Slash command: %s", SLACK_SLASH_COMMAND)
    logger.info("Target Slack channel: %s", SLACK_CHANNEL_ID)

    # Start the background scheduler (if configured)
    scheduler = start_scheduler()

    try:
        # Start the Socket Mode handler — this blocks the main thread and
        # maintains the WebSocket connection to Slack.
        logger.info("Connecting to Slack via Socket Mode...")
        handler = SocketModeHandler(app, SLACK_APP_TOKEN)
        handler.start()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        if scheduler and scheduler.running:
            scheduler.shutdown(wait=False)
            logger.info("Scheduler shut down.")