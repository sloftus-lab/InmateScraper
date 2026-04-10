"""
Penobscot County Sheriff Inmate Roster Scraper
Tracks who gets booked and when, appending new entries to a CSV.

No dependencies beyond the Python standard library.

Run:   python3 scraper.py
"""

import csv
import json
import logging
import os
import re
import smtplib

# Load .env if present (local dev)
try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html.parser import HTMLParser
from pathlib import Path
from time import sleep

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

API_BASE  = "https://blogapi.myocv.com/prod/paginatedBlog/a53704401"
API_KEY   = "SbRiICL5la3daytBtRL2K26xorlmbPXZ3jPQLVzR"
API_LIMIT = 100
CSV_FILE  = Path(__file__).parent / "inmates.csv"
LOG_FILE  = Path(__file__).parent / "scraper.log"

# Email — read from environment variables (set in .env locally, GitHub Secrets in Actions)
EMAIL_FROM     = os.environ.get("INMATE_EMAIL_FROM", "")
EMAIL_PASSWORD = os.environ.get("INMATE_EMAIL_PASSWORD", "")
EMAIL_TO       = os.environ.get("INMATE_EMAIL_TO", "")   # comma-separated
SMTP_HOST      = "smtp.gmail.com"
SMTP_PORT      = 587

HEADERS = {
    "x-api-key":  API_KEY,
    "referer":    "https://www.penobscot-sheriff.net/",
    "user-agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "accept": "application/json, text/plain, */*",
}

CSV_FIELDS = [
    "scraped_at",
    "inmate_id",
    "name",
    "booking_date",
    "booking_time",
    "height",
    "weight",
    "gender",
    "race",
    "age",
    "eye_color",
    "hair_color",
    "custody_status",
    "release_date",
    "arresting_agency",
]

log = logging.getLogger(__name__)


def configure_logging(also_stream: bool = True):
    handlers = [logging.FileHandler(LOG_FILE)]
    if also_stream:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
        force=True,
    )


# ---------------------------------------------------------------------------
# HTML → plain text
# ---------------------------------------------------------------------------

class _TextStripper(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        if tag == "br":
            self.parts.append("\n")

    def text(self) -> str:
        return "".join(self.parts)


def _strip_html(html: str) -> str:
    s = _TextStripper()
    s.feed(html)
    return s.text()


def _field(text: str, label: str) -> str:
    m = re.search(rf"(?i){re.escape(label)}:\s*(.+)", text)
    return m.group(1).strip() if m else ""


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _get_page(page: int) -> dict:
    url = (
        f"{API_BASE}"
        f"?blogKey=inmates"
        f"&limit={API_LIMIT}"
        f"&sort=dateDesc"
        f"&type=integration"
        f"&translation=default"
        f"&page={page}"
    )
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def fetch_all_inmates() -> list[dict]:
    all_records: list[dict] = []
    page = 1
    while True:
        log.info("Fetching page %d …", page)
        try:
            data = _get_page(page)
        except urllib.error.HTTPError as e:
            log.error("HTTP %d on page %d — stopping.", e.code, page)
            break
        except Exception as e:
            log.error("Error on page %d: %s", page, e)
            break

        entries = data.get("entries", [])
        pagination = data.get("pagination", {})
        log.info("  %d records  (total: %s)", len(entries), pagination.get("totalEntries", "?"))
        all_records.extend(entries)

        if not pagination.get("next"):
            break
        page += 1
        sleep(0.1)

    return all_records


# ---------------------------------------------------------------------------
# Record normalisation
# ---------------------------------------------------------------------------

def parse_record(raw: dict, scraped_at: str) -> dict:
    name      = raw.get("titleWithFirst") or raw.get("title", "")
    inmate_id = raw.get("inmateID", "")
    custody   = raw.get("custody_status_cd", "")

    sec = (raw.get("date") or {}).get("sec")
    if sec:
        dt = datetime.fromtimestamp(sec, tz=timezone.utc).astimezone()
        booking_date = dt.strftime("%Y-%m-%d")
        booking_time = dt.strftime("%H:%M %Z")
    else:
        booking_date = booking_time = ""

    text = _strip_html(raw.get("content", ""))

    booked_str = _field(text, "Booked Date")
    if booked_str:
        m = re.match(r"(\d{2}/\d{2}/\d{4})\s+(\d{2}:\d{2}:\d{2})\s*(\w+)?", booked_str)
        if m:
            booking_date = datetime.strptime(m.group(1), "%m/%d/%Y").strftime("%Y-%m-%d")
            booking_time = m.group(2) + (" " + m.group(3) if m.group(3) else "")

    charge_array = raw.get("chargeArray", [])
    if isinstance(charge_array, list) and charge_array and isinstance(charge_array[0], dict):
        charges = "; ".join(
            c.get("chargeDescription") or c.get("charge", "")
            for c in charge_array if isinstance(c, dict)
        )
    else:
        charges = _field(text, "Charge") or _field(text, "Charges") or _field(text, "Offense")

    arresting = _field(text, "Arresting Agency")
    if arresting.lower() == "currently unavailable":
        arresting = ""

    return {
        "scraped_at":       scraped_at,
        "inmate_id":        inmate_id,
        "name":             name,
        "booking_date":     booking_date,
        "booking_time":     booking_time,
        "height":           _field(text, "Height"),
        "weight":           _field(text, "Weight"),
        "gender":           _field(text, "Gender"),
        "race":             _field(text, "Race"),
        "age":              _field(text, "Age"),
        "eye_color":        _field(text, "Eye Color"),
        "hair_color":       _field(text, "Hair Color"),
        "custody_status":   custody,
        "release_date":     "",
        "arresting_agency": arresting,
    }


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def load_existing_ids() -> set[str]:
    seen: set[str] = set()
    if not CSV_FILE.exists():
        return seen
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            iid = row.get("inmate_id", "").strip()
            if iid:
                seen.add(iid)
    return seen


def append_rows(rows: list[dict]) -> int:
    is_new = not CSV_FILE.exists()
    with open(CSV_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def mark_released(active_ids: set[str]) -> list[dict]:
    """
    Compare active_ids (currently in jail) against CSV rows marked IN.
    Any row marked IN that is no longer active gets stamped OUT with today's date.
    Returns list of newly-released rows.
    """
    if not CSV_FILE.exists():
        return []

    today = datetime.now().strftime("%Y-%m-%d")
    rows = []
    released = []

    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            iid = row.get("inmate_id", "").strip()
            if (
                iid
                and row.get("custody_status") == "IN"
                and iid not in active_ids
                and not row.get("release_date")
            ):
                row["custody_status"] = "OUT"
                row["release_date"]   = today
                released.append(row)
            rows.append(row)

    if released:
        # Rewrite the whole CSV with updated rows
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        log.info("Marked %d inmate(s) as released.", len(released))
        for r in released[:5]:
            log.info("  Released: %s (booked %s)", r["name"], r["booking_date"])
        if len(released) > 5:
            log.info("  … and %d more", len(released) - 5)

    return released


# ---------------------------------------------------------------------------
# Static HTML generator
# ---------------------------------------------------------------------------

HTML_FILE = Path(__file__).parent / "roster.html"

def generate_html():
    """Write a fully self-contained roster.html from the current CSV."""
    inmates = []
    if CSV_FILE.exists():
        with open(CSV_FILE, newline="", encoding="utf-8") as f:
            inmates = list(csv.DictReader(f))

    JAIL_CAPACITY = 157
    today = datetime.now().strftime("%Y-%m-%d")
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    total = len(inmates)
    in_custody   = sum(1 for r in inmates if r.get("custody_status") == "IN")
    boarded_out  = sum(1 for r in inmates if r.get("custody_status") == "BO")
    booked_today = sum(1 for r in inmates if r.get("booking_date") == today)
    capacity_pct = round(in_custody / JAIL_CAPACITY * 100)
    cap_color = "danger" if capacity_pct >= 100 else "warning" if capacity_pct >= 80 else "success"
    cap_width = min(capacity_pct, 100)
    boarded_note = f" &nbsp;·&nbsp; {boarded_out} boarded out" if boarded_out else ""

    # Newest scraped_at timestamp = records added in the last run
    last_scraped = max((r.get("scraped_at", "") for r in inmates), default="")

    rows_html = []
    for r in inmates:
        is_today = r.get("booking_date") == today
        is_new   = r.get("scraped_at", "") == last_scraped
        row_class = "today-row" if is_today else ("new-row" if is_new else "")
        status = r.get("custody_status", "")
        status_badge = (
            '<span class="badge bg-danger">IN</span>'                                    if status == "IN"
            else '<span class="badge bg-success">OUT</span>'                             if status == "OUT"
            else '<span class="badge" style="background:#6f42c1">BOARDED OUT</span>'    if status == "BO"
            else f'<span class="badge bg-secondary">{status}</span>'                     if status
            else ""
        )
        def e(v): return str(v).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
        rows_html.append(f"""
        <tr class="{row_class}">
          <td class="fw-semibold">{e(r.get('name',''))}</td>
          <td>{e(r.get('booking_date',''))}</td>
          <td>{e(r.get('booking_time',''))}</td>
          <td>{e(r.get('age',''))}</td>
          <td>{e(r.get('gender',''))}</td>
          <td>{e(r.get('race',''))}</td>
          <td>{e(r.get('height',''))}</td>
          <td>{e(r.get('weight',''))}</td>
          <td>{e(r.get('eye_color',''))}</td>
          <td>{e(r.get('hair_color',''))}</td>
          <td>{status_badge}</td>
          <td>{e(r.get('release_date',''))}</td>
          <td>{e(r.get('arresting_agency',''))}</td>
          <td class="text-muted small">{e(r.get('scraped_at',''))}</td>
        </tr>""")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
  <meta http-equiv="Pragma" content="no-cache">
  <meta http-equiv="Expires" content="0">
  <title>Penobscot County Inmate Roster</title>
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
  <link href="https://cdn.datatables.net/2.0.7/css/dataTables.bootstrap5.min.css" rel="stylesheet">
  <style>
    body {{ background:#f4f6f9; }}
    .navbar {{ background:#1a2e44 !important; }}
    .stat-card {{ border:none; border-radius:10px; }}
    .stat-card .display-6 {{ font-weight:700; }}
    tr.today-row td {{ background:#fff8e1 !important; }}
    tr.new-row td {{ background:#e8f5e9 !important; }}
    th {{ white-space:nowrap; }}
    :root {{ --bs-success:#198754; --bs-warning:#ffc107; --bs-danger:#dc3545; }}
  </style>
</head>
<body>
<nav class="navbar navbar-dark mb-4">
  <div class="container-fluid">
    <span class="navbar-brand fw-bold">&#x1F512; Penobscot County Sheriff — Inmate Roster</span>
    <span class="text-light small">Generated: {generated_at}</span>
  </div>
</nav>
<div class="container-fluid px-4">
  <div class="row g-3 mb-4">
    <div class="col-sm-3">
      <div class="card stat-card shadow-sm text-center p-3">
        <div class="text-muted small mb-1">Total Records</div>
        <div class="display-6 text-primary">{total}</div>
      </div>
    </div>
    <div class="col-sm-3">
      <div class="card stat-card shadow-sm text-center p-3">
        <div class="text-muted small mb-1">Currently In Custody</div>
        <div class="display-6 text-danger">{in_custody}</div>
      </div>
    </div>
    <div class="col-sm-3">
      <div class="card stat-card shadow-sm text-center p-3">
        <div class="text-muted small mb-1">Booked Today</div>
        <div class="display-6 text-success">{booked_today}</div>
      </div>
    </div>
    <div class="col-sm-3">
      <div class="card stat-card shadow-sm text-center p-3">
        <div class="text-muted small mb-1">Jail Capacity ({JAIL_CAPACITY})</div>
        <div class="display-6 text-{cap_color}">{capacity_pct}%</div>
        <div style="background:#e9ecef;border-radius:6px;height:10px;margin-top:8px">
          <div style="background:var(--bs-{cap_color});width:{cap_width}%;height:10px;border-radius:6px"></div>
        </div>
        <div class="text-muted small mt-1">{in_custody} / {JAIL_CAPACITY}{boarded_note}</div>
      </div>
    </div>
  </div>
  <div class="alert alert-info py-2 d-flex justify-content-between align-items-center">
    <span>&#x1F551; Last updated: <strong>{generated_at}</strong></span>
    <span class="text-muted small">Auto-refreshes every hour &nbsp;·&nbsp; <a href="https://github.com/sloftus-lab/InmateScraper/actions" target="_blank" class="alert-link">Run manually on GitHub</a></span>
  </div>
  <div class="card shadow-sm mb-5">
    <div class="card-header"><strong>Inmate Records</strong></div>
    <div class="card-body p-0">
      <div class="table-responsive">
        <table id="inmates-table" class="table table-hover table-sm mb-0 align-middle">
          <thead class="table-dark">
            <tr>
              <th>Name</th><th>Booking Date</th><th>Time</th><th>Age</th>
              <th>Gender</th><th>Race</th><th>Height</th><th>Weight</th>
              <th>Eyes</th><th>Hair</th><th>Status</th><th>Released</th>
              <th>Arresting Agency</th><th>Scraped At</th>
            </tr>
          </thead>
          <tbody>{''.join(rows_html)}</tbody>
        </table>
      </div>
    </div>
  </div>
  <div class="pb-4">
    <small class="text-muted">
      <span class="badge" style="background:#fff8e1;color:#333;border:1px solid #ddd">Yellow</span> = booked today &nbsp;
      <span class="badge" style="background:#e8f5e9;color:#333;border:1px solid #ddd">Green</span> = new since last run
    </small>
  </div>
</div>
<script src="https://code.jquery.com/jquery-3.7.1.min.js"></script>
<script src="https://cdn.datatables.net/2.0.7/js/dataTables.min.js"></script>
<script src="https://cdn.datatables.net/2.0.7/js/dataTables.bootstrap5.min.js"></script>
<script>
  $('#inmates-table').DataTable({{
    order: [[1,'desc'],[2,'desc']],
    pageLength: 25,
    language: {{ search: 'Filter:' }}
  }});
</script>
</body>
</html>"""

    HTML_FILE.write_text(html, encoding="utf-8")
    log.info("HTML roster written: %s", HTML_FILE.resolve())


# ---------------------------------------------------------------------------
# Email notifications
# ---------------------------------------------------------------------------

def send_email(new_rows: list[dict], released_rows=None):
    """Send an email listing new bookings and/or releases. Skips silently if credentials missing."""
    if not all([EMAIL_FROM, EMAIL_PASSWORD, EMAIL_TO]):
        log.debug("Email not configured — skipping.")
        return

    released_rows = released_rows or []
    recipients = [r.strip() for r in EMAIL_TO.split(",") if r.strip()]
    new_count = len(new_rows)
    rel_count = len(released_rows)

    parts = []
    if new_count:
        parts.append(f"{new_count} new booking{'s' if new_count != 1 else ''}")
    if rel_count:
        parts.append(f"{rel_count} release{'s' if rel_count != 1 else ''}")
    subject = f"Penobscot Jail: {', '.join(parts)}" if parts else "Penobscot Jail: update"

    def booking_table(rows):
        row_cells = "".join(f"""
        <tr>
          <td style="padding:6px 12px;border-bottom:1px solid #eee"><strong>{r['name']}</strong></td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee">{r['booking_date']}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee">{r['booking_time']}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee">{r['age']}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee">{r['gender']}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee">{r['height']}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee">{r['weight']}</td>
        </tr>""" for r in rows)
        return f"""
      <table style="border-collapse:collapse;width:100%;font-size:14px">
        <thead>
          <tr style="background:#1a2e44;color:white">
            <th style="padding:8px 12px;text-align:left">Name</th>
            <th style="padding:8px 12px;text-align:left">Booked</th>
            <th style="padding:8px 12px;text-align:left">Time</th>
            <th style="padding:8px 12px;text-align:left">Age</th>
            <th style="padding:8px 12px;text-align:left">Gender</th>
            <th style="padding:8px 12px;text-align:left">Height</th>
            <th style="padding:8px 12px;text-align:left">Weight</th>
          </tr>
        </thead>
        <tbody>{row_cells}</tbody>
      </table>"""

    def release_table(rows):
        row_cells = "".join(f"""
        <tr>
          <td style="padding:6px 12px;border-bottom:1px solid #eee"><strong>{r['name']}</strong></td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee">{r['booking_date']}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee">{r['release_date']}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee">{r['age']}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee">{r['gender']}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee">{r.get('arresting_agency','')}</td>
        </tr>""" for r in rows)
        return f"""
      <table style="border-collapse:collapse;width:100%;font-size:14px">
        <thead>
          <tr style="background:#4a6741;color:white">
            <th style="padding:8px 12px;text-align:left">Name</th>
            <th style="padding:8px 12px;text-align:left">Booked</th>
            <th style="padding:8px 12px;text-align:left">Released</th>
            <th style="padding:8px 12px;text-align:left">Age</th>
            <th style="padding:8px 12px;text-align:left">Gender</th>
            <th style="padding:8px 12px;text-align:left">Agency</th>
          </tr>
        </thead>
        <tbody>{row_cells}</tbody>
      </table>"""

    sections = []
    if new_count:
        sections.append(f"""
      <h2 style="color:#1a2e44;margin-top:0">&#x1F512; New Bookings ({new_count})</h2>
      {booking_table(new_rows)}""")
    if rel_count:
        sections.append(f"""
      <h2 style="color:#4a6741;margin-top:28px">&#x2705; Releases ({rel_count})</h2>
      {release_table(released_rows)}""")

    body = f"""
    <html><body style="font-family:sans-serif;color:#333;max-width:800px;margin:0 auto;padding:20px">
      <p style="color:#666;font-size:13px">Detected at {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
      {''.join(sections)}
      <p style="margin-top:24px;font-size:13px">
        <a href="https://sloftus-lab.github.io/InmateScraper/" style="color:#1a2e44">View full roster →</a>
      </p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.starttls()
            smtp.login(EMAIL_FROM, EMAIL_PASSWORD)
            smtp.sendmail(EMAIL_FROM, recipients, msg.as_string())
        log.info("Email sent to %s", ", ".join(recipients))
    except Exception as e:
        log.error("Email failed: %s", e)


# ---------------------------------------------------------------------------
# macOS notifications
# ---------------------------------------------------------------------------

def notify(title: str, message: str):
    """Send a macOS notification. Silently skips on non-Mac platforms."""
    try:
        script = (
            f'display notification "{message}" '
            f'with title "{title}" '
            f'sound name "Funk"'
        )
        subprocess.run(["osascript", "-e", script], check=False, timeout=5)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Core run — importable by app.py
# ---------------------------------------------------------------------------

def run_scrape() -> dict:
    """
    Run one full scrape cycle. Returns a summary dict:
      {total_fetched, new_count, new_rows, error}
    """
    log.info("=== Scrape started: %s ===", datetime.now().isoformat(timespec="seconds"))
    try:
        raw_records = fetch_all_inmates()
    except Exception as e:
        msg = f"Fetch failed: {e}"
        log.error(msg)
        return {"total_fetched": 0, "new_count": 0, "new_rows": [], "error": msg}

    if not raw_records:
        return {"total_fetched": 0, "new_count": 0, "new_rows": [], "error": "No records returned"}

    log.info("Total records from API: %d", len(raw_records))
    existing_ids = load_existing_ids()
    scraped_at   = datetime.now().isoformat(timespec="seconds")

    new_rows: list[dict] = []
    for raw in raw_records:
        row = parse_record(raw, scraped_at)
        iid = row["inmate_id"]
        if not iid or iid in existing_ids:
            continue
        new_rows.append(row)
        existing_ids.add(iid)

    if new_rows:
        append_rows(new_rows)
        log.info("New bookings added: %d", len(new_rows))
        for row in new_rows[:5]:
            log.info(
                "  %-30s  booked: %s %s",
                row["name"], row["booking_date"], row["booking_time"],
            )
        if len(new_rows) > 5:
            log.info("  … and %d more", len(new_rows) - 5)
    else:
        log.info("No new bookings.")

    # Check for releases — anyone marked IN who isn't in the current API response
    active_ids = {raw.get("inmateID", "") for raw in raw_records if raw.get("inmateID")}
    released_rows = mark_released(active_ids)

    generate_html()

    return {
        "total_fetched":  len(raw_records),
        "new_count":      len(new_rows),
        "new_rows":       new_rows,
        "released_count": len(released_rows),
        "released_rows":  released_rows,
        "error":          None,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    configure_logging(also_stream=True)
    result = run_scrape()
    if result["error"]:
        sys.exit(1)
    n = result["new_count"]
    r = result.get("released_count", 0)
    if n or r:
        notify_parts = []
        if n:
            names = ", ".join(row["name"] for row in result["new_rows"][:3])
            suffix = f" + {n - 3} more" if n > 3 else ""
            notify_parts.append(f"{n} new: {names}{suffix}")
        if r:
            notify_parts.append(f"{r} released")
        notify("Penobscot Inmate Roster", " · ".join(notify_parts))
        send_email(result["new_rows"], result.get("released_rows", []))


def test_email():
    """Send a test email without running the scraper."""
    configure_logging(also_stream=True)
    log.info("Sending test email to %s", EMAIL_TO)
    fake_bookings = [{
        "name": "TEST BOOKING",
        "booking_date": datetime.now().strftime("%Y-%m-%d"),
        "booking_time": datetime.now().strftime("%H:%M EDT"),
        "age": "35", "gender": "M", "height": "5 ft 10in(s)", "weight": "180 lbs",
    }]
    fake_releases = [{
        "name": "TEST RELEASE",
        "booking_date": "2026-04-01",
        "release_date": datetime.now().strftime("%Y-%m-%d"),
        "age": "28", "gender": "F", "arresting_agency": "Bangor PD",
    }]
    send_email(fake_bookings, fake_releases)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--test-email":
        test_email()
    else:
        main()
