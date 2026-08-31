"""
Scraper Watchdog

GitHub's `schedule:` trigger is best-effort -- during a platform incident it
can silently drop an hourly run with no failed job left behind to notice
(confirmed to happen for hours at a stretch on 2026-08-26/27). This polls
the actual run history instead of trusting the schedule to have fired, and
emails an alert if too much time has passed since the last successful scrape.

Run:   python3 watchdog.py
"""

import json
import os
import smtplib
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText

REPO      = os.environ["GITHUB_REPOSITORY"]
TOKEN     = os.environ["GITHUB_TOKEN"]
THRESHOLD = int(os.environ.get("WATCHDOG_THRESHOLD_MINUTES", "150"))

EMAIL_FROM     = os.environ.get("INMATE_EMAIL_FROM", "")
EMAIL_PASSWORD = os.environ.get("INMATE_EMAIL_PASSWORD", "")
EMAIL_TO       = os.environ.get("INMATE_EMAIL_TO", "")


def latest_successful_scrape() -> dict | None:
    url = (
        f"https://api.github.com/repos/{REPO}/actions/workflows/scrape.yml/runs"
        f"?status=success&per_page=1"
    )
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {TOKEN}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "inmate-scraper-watchdog",
    })
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.load(resp)
    runs = data.get("workflow_runs", [])
    return runs[0] if runs else None


def send_alert(minutes_since: float, last_run_url: str) -> None:
    if not all([EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO]):
        print("Email not configured -- skipping alert email.")
        return

    recipients = [r.strip() for r in EMAIL_TO.split(",") if r.strip()]
    since_desc = "no successful run found" if minutes_since == float("inf") else f"{minutes_since:.0f} minutes"
    body = (
        f"No successful \"Scrape Inmate Roster\" run in {since_desc} "
        f"(alert threshold: {THRESHOLD} minutes).\n\n"
        f"Last successful run: {last_run_url}\n\n"
        f"This usually means GitHub's scheduled trigger silently didn't fire. "
        f"Check https://github.com/{REPO}/actions/workflows/scrape.yml and "
        f"trigger a run manually if needed."
    )
    msg = MIMEText(body)
    msg["Subject"] = f"Inmate scraper watchdog: {since_desc} since last successful scrape"
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(recipients)

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, recipients, msg.as_string())
    print(f"Alert email sent to {', '.join(recipients)}")


def main() -> None:
    # Deliberately exits 0 whenever it successfully detects and alerts on a
    # gap -- that's the watchdog doing its job, not a CI failure. Exiting 1
    # here would make GitHub also fire its own "run failed" notification on
    # top of the alert email below, doubling up on every real gap. A non-zero
    # exit is reserved for the watchdog itself erroring out (see the
    # unhandled-exception path, which already exits non-zero on its own).
    run = latest_successful_scrape()
    if not run:
        print("No successful runs found at all.")
        send_alert(float("inf"), f"https://github.com/{REPO}/actions/workflows/scrape.yml")
        return

    created_at = datetime.fromisoformat(run["created_at"].replace("Z", "+00:00"))
    minutes_since = (datetime.now(timezone.utc) - created_at).total_seconds() / 60
    print(f"Last successful scrape: {run['created_at']} ({minutes_since:.0f} min ago)")

    if minutes_since > THRESHOLD:
        send_alert(minutes_since, run["html_url"])
        return

    print("OK — within threshold.")


if __name__ == "__main__":
    main()
