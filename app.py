"""
Penobscot County Sheriff Inmate Roster — Web Dashboard
Run: python3 app.py
Open: http://localhost:5001
"""

import csv
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request, make_response, redirect, url_for, send_file

import scraper as sc

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
CSV_FILE = BASE_DIR / "inmates.csv"
LOG_FILE = BASE_DIR / "scraper.log"

sc.configure_logging(also_stream=False)   # log to file, not stdout

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False


@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route("/api/<path:_>", methods=["OPTIONS"])
def options_handler(_):
    return make_response("", 204)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_csv() -> list[dict]:
    if not CSV_FILE.exists():
        return []
    with open(CSV_FILE, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_log_tail(n: int = 60) -> str:
    if not LOG_FILE.exists():
        return ""
    lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    return "\n".join(lines[-n:])


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

JAIL_CAPACITY = 157

def _render_index(run_msg=None, run_error=False):
    inmates = read_csv()
    today = datetime.now().strftime("%Y-%m-%d")
    total        = len(inmates)
    booked_today = sum(1 for r in inmates if r.get("booking_date") == today)
    in_custody   = sum(1 for r in inmates if r.get("custody_status") == "IN")
    boarded_out  = sum(1 for r in inmates if r.get("custody_status") == "BO")
    capacity_pct = round(in_custody / JAIL_CAPACITY * 100)
    last_scraped = max((r.get("scraped_at", "") for r in inmates), default=None)
    return render_template(
        "index.html",
        inmates=inmates,
        total=total,
        booked_today=booked_today,
        in_custody=in_custody,
        boarded_out=boarded_out,
        capacity_pct=capacity_pct,
        last_scraped=last_scraped,
        last_run={"at": last_scraped, "total_fetched": total, "new_count": 0, "error": None},
        today=today,
        run_msg=run_msg,
        run_error=run_error,
    )


@app.route("/")
def index():
    return _render_index()


@app.route("/run", methods=["POST"])
def run():
    result = sc.run_scrape()
    n = result["new_count"]
    if result["error"]:
        return _render_index(run_msg=f"Error: {result['error']}", run_error=True)
    if n:
        names = ", ".join(r["name"] for r in result["new_rows"][:3])
        suffix = f" + {n - 3} more" if n > 3 else ""
        sc.notify("Penobscot Inmate Roster", f"{n} new booking{'s' if n != 1 else ''}: {names}{suffix}")
        return _render_index(run_msg=f"✓ {n} new booking{'s' if n != 1 else ''} found: {names}{suffix}")
    return _render_index(run_msg=f"✓ Done — fetched {result['total_fetched']}, no new bookings")


@app.route("/roster")
def roster():
    resp = make_response(sc.HTML_FILE.read_text(encoding="utf-8"))
    resp.headers["Content-Type"] = "text/html"
    resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.route("/api/inmates")
def api_inmates():
    return jsonify(read_csv())


@app.route("/api/logs")
def api_logs():
    return jsonify({"log": read_log_tail(80)})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    print(f"\n  Inmate Roster Dashboard → http://localhost:{port}\n")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
