#!/usr/bin/env python3
"""
FEC Tracker
Monitors the FEC (via the openFEC API) for Maine election activity and
sends email alerts via Gmail SMTP when something new shows up:

  1. New Form 1 filings (new committee registrations)
     https://www.fec.gov/data/filings/?filing_form=F1&data_type=processed&state=ME
  2. New committees registered in the state
     https://www.fec.gov/data/committees/?state=ME
  3. New candidates entering the state's Senate race for the given cycle
     https://www.fec.gov/data/elections/senate/ME/2026/
"""

import json
import os
import smtplib
import sys
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

# ---------------------------------------------------------------------------
# Config (from .env)
# ---------------------------------------------------------------------------
FEC_API_KEY    = os.environ["FEC_API_KEY"]
WATCH_STATE    = os.getenv("FEC_STATE", "ME").upper()
SENATE_CYCLE   = int(os.getenv("FEC_SENATE_CYCLE", "2026"))
EMAIL_FROM     = os.environ["EMAIL_FROM"]
EMAIL_PASSWORD = os.environ["EMAIL_PASSWORD"]
EMAIL_TO       = os.environ["EMAIL_TO"]
SMTP_HOST      = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT      = int(os.getenv("SMTP_PORT", "587"))

STATE_FILE = Path(os.getenv("FEC_STATE_FILE", "fec_state.json"))
BASE_URL   = "https://api.open.fec.gov/v1"


# ---------------------------------------------------------------------------
# State persistence — tracks which records we've already alerted on
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ---------------------------------------------------------------------------
# openFEC API
# ---------------------------------------------------------------------------

def _get(path: str, params: dict, retries: int = 4, backoff: int = 15) -> dict:
    url = f"{BASE_URL}{path}"
    query = {**params, "api_key": FEC_API_KEY}
    err: Exception = RuntimeError("unreachable")
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=query, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as e:
            if resp.status_code < 500:
                raise  # don't retry client errors (bad params, rate limit, etc.)
            err = e
        except requests.RequestException as e:
            err = e
        if attempt < retries:
            print(f"Request to {path} failed (attempt {attempt}/{retries}): {err} — retrying in {backoff}s...", file=sys.stderr)
            time.sleep(backoff)
            backoff *= 2
        else:
            raise err


# ---------------------------------------------------------------------------
# Check 1: New Form 1 filings (new committee registrations) in WATCH_STATE
# ---------------------------------------------------------------------------
#
# NOTE: the /filings/ endpoint's `state` param filters on the state where a
# *candidate* runs for office, not the filer's registered address. F1
# (Statement of Organization) filings are often submitted before any
# candidate is attached — e.g. exploratory committees — so `state` is null
# on those records and an FEC-side `state=ME` filter silently excludes them.
# Instead we pull F1 filings nationwide and keep only the ones whose
# committee_id belongs to a committee registered in WATCH_STATE (from
# check_committees, which filters on the committee's actual address and
# does catch exploratory committees correctly).

def check_filings(state: dict, watch_state_committee_ids: set[str]) -> list[dict]:
    key = "filings"
    first_run = key not in state
    seen = set(state.get(key, {}).get("seen_file_numbers", []))

    data = _get("/filings/", {
        "form_type": "F1",
        "sort": "-receipt_date",
        "per_page": 100,
    })
    results = [r for r in data.get("results", []) if r["committee_id"] in watch_state_committee_ids]

    new_items = [] if first_run else [r for r in results if r["file_number"] not in seen]

    seen.update(r["file_number"] for r in results)
    state[key] = {"seen_file_numbers": list(seen)[-1000:]}
    return new_items


# ---------------------------------------------------------------------------
# Check 2: New committees registered in WATCH_STATE
#
# Fetches the *complete* set of committees registered in WATCH_STATE (paginated,
# not just a recent window) so it can also serve as the authoritative lookup
# check_filings() needs to attribute F1 filings to WATCH_STATE.
# ---------------------------------------------------------------------------

def check_committees(state: dict) -> tuple[list[dict], set[str]]:
    key = "committees"
    first_run = key not in state
    seen = set(state.get(key, {}).get("seen_committee_ids", []))

    results = []
    page = 1
    while True:
        data = _get("/committees/", {"state": WATCH_STATE, "per_page": 100, "page": page})
        results.extend(data.get("results", []))
        if page >= data.get("pagination", {}).get("pages", page):
            break
        page += 1

    current_ids = {r["committee_id"] for r in results}
    new_items = [] if first_run else [r for r in results if r["committee_id"] not in seen]

    state[key] = {"seen_committee_ids": sorted(current_ids)}
    return new_items, current_ids


# ---------------------------------------------------------------------------
# Check 3: New candidates in the WATCH_STATE Senate race for SENATE_CYCLE
# ---------------------------------------------------------------------------

def check_senate_race(state: dict) -> list[dict]:
    key = "senate_race"
    first_run = key not in state
    seen = set(state.get(key, {}).get("seen_candidate_ids", []))

    data = _get("/elections/", {
        "state": WATCH_STATE,
        "office": "senate",
        "cycle": SENATE_CYCLE,
        "per_page": 100,
    })
    results = data.get("results", [])

    new_items = [] if first_run else [r for r in results if r["candidate_id"] not in seen]

    seen.update(r["candidate_id"] for r in results)
    state[key] = {"seen_candidate_ids": sorted(seen)}
    return new_items


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def build_email(filings: list[dict], committees: list[dict], candidates: list[dict]):
    recipients = [r.strip() for r in EMAIL_TO.split(",")]
    total = len(filings) + len(committees) + len(candidates)
    subject = f"[FEC Alert] {total} update{'s' if total != 1 else ''} for {WATCH_STATE}"

    lines = [f"FEC Tracker — {WATCH_STATE}", f"Checked at: {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
    html_sections = ""

    if filings:
        lines.append(f"NEW FORM 1 FILINGS ({len(filings)})")
        lines.append("-" * 60)
        rows = ""
        for f in filings:
            name = f.get("committee_name") or f.get("candidate_name") or "N/A"
            lines += [
                f"Committee:    {name}",
                f"Committee ID: {f.get('committee_id', 'N/A')}",
                f"Received:     {f.get('receipt_date', 'N/A')}",
                f"Document:     {f.get('document_description', 'N/A')}",
                f"PDF:          {f.get('pdf_url', 'N/A')}",
                "-" * 60,
            ]
            rows += (
                f"<tr><td>{name}</td><td>{f.get('committee_id', '')}</td>"
                f"<td>{f.get('receipt_date', '')}</td>"
                f"<td><a href=\"{f.get('pdf_url', '')}\">view</a></td></tr>"
            )
        html_sections += f"""
        <h3>New Form 1 filings ({len(filings)})</h3>
        <table border="1" cellpadding="4" cellspacing="0" style="border-collapse:collapse;font-family:monospace;font-size:13px;">
          <thead style="background:#e0e0e0;"><tr><th>Committee</th><th>ID</th><th>Received</th><th>Filing</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>"""
        lines.append("")

    if committees:
        lines.append(f"NEW COMMITTEES REGISTERED ({len(committees)})")
        lines.append("-" * 60)
        rows = ""
        for c in committees:
            lines += [
                f"Name:         {c.get('name', 'N/A')}",
                f"ID:           {c.get('committee_id', 'N/A')}",
                f"Type:         {c.get('committee_type_full', 'N/A')}",
                f"Party:        {c.get('party_full', 'N/A')}",
                f"First filed:  {c.get('first_file_date', 'N/A')}",
                "-" * 60,
            ]
            rows += (
                f"<tr><td>{c.get('name', '')}</td><td>{c.get('committee_id', '')}</td>"
                f"<td>{c.get('committee_type_full', '')}</td><td>{c.get('party_full', '')}</td>"
                f"<td>{c.get('first_file_date', '')}</td></tr>"
            )
        html_sections += f"""
        <h3>New committees registered ({len(committees)})</h3>
        <table border="1" cellpadding="4" cellspacing="0" style="border-collapse:collapse;font-family:monospace;font-size:13px;">
          <thead style="background:#e0e0e0;"><tr><th>Name</th><th>ID</th><th>Type</th><th>Party</th><th>First filed</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>"""
        lines.append("")

    if candidates:
        lines.append(f"NEW CANDIDATES — {WATCH_STATE} SENATE {SENATE_CYCLE} ({len(candidates)})")
        lines.append("-" * 60)
        rows = ""
        for cand in candidates:
            receipts = cand.get("total_receipts") or 0
            lines += [
                f"Name:       {cand.get('candidate_name', 'N/A')}",
                f"ID:         {cand.get('candidate_id', 'N/A')}",
                f"Party:      {cand.get('party_full', 'N/A')}",
                f"Status:     {cand.get('incumbent_challenge_full', 'N/A')}",
                f"Receipts:   ${receipts:,.2f}",
                "-" * 60,
            ]
            rows += (
                f"<tr><td>{cand.get('candidate_name', '')}</td><td>{cand.get('candidate_id', '')}</td>"
                f"<td>{cand.get('party_full', '')}</td><td>{cand.get('incumbent_challenge_full', '')}</td>"
                f"<td>${receipts:,.2f}</td></tr>"
            )
        html_sections += f"""
        <h3>New candidates &mdash; {WATCH_STATE} Senate {SENATE_CYCLE} ({len(candidates)})</h3>
        <table border="1" cellpadding="4" cellspacing="0" style="border-collapse:collapse;font-family:monospace;font-size:13px;">
          <thead style="background:#e0e0e0;"><tr><th>Name</th><th>ID</th><th>Party</th><th>Status</th><th>Total receipts</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>"""

    html = f"""
    <html><body>
    <h2>FEC Tracker &mdash; {WATCH_STATE}</h2>
    <p>Checked at {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
    {html_sections}
    </body></html>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_FROM
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText("\n".join(lines), "plain"))
    msg.attach(MIMEText(html, "html"))
    return msg, recipients


def send_email(msg: MIMEMultipart, recipients: list[str]) -> None:
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(EMAIL_FROM, EMAIL_PASSWORD)
        server.sendmail(EMAIL_FROM, recipients, msg.as_string())
    print(f"Email sent to: {', '.join(recipients)}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    state = load_state()
    is_first_run = not state

    print(f"Checking FEC for {WATCH_STATE} updates (Senate cycle {SENATE_CYCLE})...")

    try:
        new_committees, watch_state_committee_ids = check_committees(state)
        new_filings = check_filings(state, watch_state_committee_ids)
        new_candidates = check_senate_race(state)
    except requests.HTTPError as e:
        print(f"API error: {e}", file=sys.stderr)
        sys.exit(1)

    if is_first_run:
        print("First run — baseline captured, no alert sent.")
    else:
        print(
            f"New filings: {len(new_filings)}, "
            f"new committees: {len(new_committees)}, "
            f"new senate candidates: {len(new_candidates)}"
        )
        if new_filings or new_committees or new_candidates:
            msg, recipients = build_email(new_filings, new_committees, new_candidates)
            send_email(msg, recipients)

    save_state(state)


if __name__ == "__main__":
    main()
