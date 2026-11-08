"""
Eddy's Typing Showdown - backend API

What this program does:
  - Serves the web page (index.html) and the sentence list (sentences.json)
  - Keeps a shared leaderboard in a small database (SQLite)

How it stops fake scores:
  The browser never tells the server its own speed. When someone starts typing,
  the server writes down the start time. When they finish, the browser sends
  what was typed, and the SERVER works out the time, accuracy and WPM itself.
  "Never trust the client" is a basic security rule.

Admin commands (run inside the container):
  python app.py list            show every name on the leaderboard
  python app.py remove "Name"   delete a name (for example, a rude one)
"""

import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone

from flask import Flask, jsonify, request

# ---------- Settings ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
DATA_DIR = os.path.join(BASE_DIR, "data")          # mounted from the server, so scores survive rebuilds
DB_PATH = os.path.join(DATA_DIR, "scores.db")
PORT = int(os.environ.get("PORT", "80"))

MIN_ACCURACY = 85          # sloppy runs don't go on the board
MAX_WPM = 200              # faster than real people manage here, so it must be a script
MIN_SECONDS = 2            # no real sentence is typed in under 2 seconds
ROUND_TIMEOUT = 300        # a round has to be finished within 5 minutes
ROUNDS_PER_WINDOW = 300    # max rounds one IP address can start...
WINDOW_SECONDS = 600       # ...in 10 minutes (stops spam)
NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{1,19}$")   # 2-20 letters, numbers, space . _ -

# Load the same 100 sentences the page uses, in the same order
with open(os.path.join(STATIC_DIR, "sentences.json"), encoding="utf-8") as f:
    _s = json.load(f)
SENTENCES = _s["windows"] + _s["hacking"]

app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 4096   # reject huge request bodies

# Rounds that have started but not finished: token -> (sentence index, start time, ip)
rounds = {}
rounds_lock = threading.Lock()
recent_starts = {}   # ip -> list of start times, for the spam limit
db_lock = threading.Lock()


# ---------- Database ----------
def db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def setup_db():
    with db_lock, db() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS scores (
                          id INTEGER PRIMARY KEY AUTOINCREMENT,
                          name TEXT NOT NULL,
                          wpm INTEGER NOT NULL,
                          accuracy INTEGER NOT NULL,
                          created TEXT NOT NULL)""")


def top_scores(limit=10):
    # Best run per name (names match no matter the capital letters)
    with db() as conn:
        rows = conn.execute("""SELECT name, MAX(wpm) AS best, COUNT(*) AS rounds
                               FROM scores GROUP BY LOWER(name)
                               ORDER BY best DESC, MIN(created) ASC
                               LIMIT ?""", (limit,)).fetchall()
    return [{"name": r["name"], "wpm": r["best"], "rounds": r["rounds"]} for r in rows]


def place_for(name):
    # Leaderboard position of this name's best score
    with db() as conn:
        best = conn.execute("SELECT MAX(wpm) FROM scores WHERE LOWER(name) = LOWER(?)", (name,)).fetchone()[0]
        ahead = conn.execute("""SELECT COUNT(*) FROM (SELECT MAX(wpm) AS b FROM scores
                                GROUP BY LOWER(name)) WHERE b > ?""", (best,)).fetchone()[0]
    return ahead + 1


# ---------- Helpers ----------
def client_ip():
    return request.remote_addr or "unknown"


def clean_name(raw):
    if not isinstance(raw, str):
        return None
    name = " ".join(raw.split())          # trim and squash extra spaces
    return name if NAME_PATTERN.match(name) else None


def forget_old_rounds(now):
    for token in [t for t, r in rounds.items() if now - r[1] > ROUND_TIMEOUT]:
        del rounds[token]


# ---------- Pages ----------
@app.get("/")
def index():
    return send_static("index.html")


@app.get("/sentences.json")
def sentences():
    return send_static("sentences.json")


def send_static(filename):
    from flask import send_from_directory
    return send_from_directory(STATIC_DIR, filename)


# ---------- API ----------
@app.get("/api/scores")
def get_scores():
    return jsonify({"top": top_scores()})


@app.post("/api/round")
def start_round():
    # Called on the first key press. The server notes the time itself.
    data = request.get_json(silent=True) or {}
    index = data.get("index")
    if not isinstance(index, int) or not 0 <= index < len(SENTENCES):
        return jsonify({"error": "Unknown sentence"}), 400

    now = time.time()
    ip = client_ip()
    with rounds_lock:
        forget_old_rounds(now)
        starts = [t for t in recent_starts.get(ip, []) if now - t < WINDOW_SECONDS]
        if len(starts) >= ROUNDS_PER_WINDOW:
            return jsonify({"error": "Too many rounds. Take a short break."}), 429
        starts.append(now)
        recent_starts[ip] = starts
        token = secrets.token_urlsafe(16)          # random, can't be guessed
        rounds[token] = (index, now, ip)
    return jsonify({"token": token})


@app.post("/api/score")
def submit_score():
    data = request.get_json(silent=True) or {}
    token, typed = data.get("token"), data.get("typed")

    # One use only: the round is removed the moment it's claimed
    with rounds_lock:
        round_info = rounds.pop(token, None) if isinstance(token, str) else None
    if round_info is None:
        return jsonify({"saved": False, "reason": "That round expired or was already used."}), 400

    index, started, _ = round_info
    seconds = time.time() - started
    target = SENTENCES[index]

    name = clean_name(data.get("name"))
    if name is None:
        return jsonify({"saved": False, "reason": "Names need 2-20 letters or numbers."}), 400
    if not isinstance(typed, str) or len(typed) != len(target):
        return jsonify({"saved": False, "reason": "The sentence wasn't finished."}), 400
    if seconds < MIN_SECONDS:
        return jsonify({"saved": False, "reason": "That was too fast to be real."}), 400

    # The server does the scoring, the same way the page does
    correct = sum(1 for a, b in zip(typed, target) if a == b)
    accuracy = round(correct / len(target) * 100)
    wpm = round((correct / 5) / (seconds / 60))

    if accuracy < MIN_ACCURACY:
        return jsonify({"saved": False, "reason": f"Only clean runs ({MIN_ACCURACY}%+ accuracy) go on the board."}), 200
    if wpm > MAX_WPM:
        return jsonify({"saved": False, "reason": "That speed isn't humanly possible."}), 400

    with db_lock, db() as conn:
        conn.execute("INSERT INTO scores (name, wpm, accuracy, created) VALUES (?, ?, ?, ?)",
                     (name, wpm, accuracy, datetime.now(timezone.utc).isoformat()))
    return jsonify({"saved": True, "name": name, "wpm": wpm, "accuracy": accuracy, "place": place_for(name)})


# ---------- Security headers on every response ----------
@app.after_request
def security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:"
    )
    return resp


# ---------- Admin commands ----------
def admin(args):
    if args[0] == "list":
        for row in top_scores(limit=1000):
            print(f'{row["name"]:<20} best {row["wpm"]} WPM, {row["rounds"]} round(s)')
    elif args[0] == "remove" and len(args) == 2:
        with db_lock, db() as conn:
            n = conn.execute("DELETE FROM scores WHERE LOWER(name) = LOWER(?)", (args[1],)).rowcount
        print(f'Removed {n} score(s) for "{args[1]}"')
    else:
        print('Usage: python app.py list   |   python app.py remove "Name"')


if __name__ == "__main__":
    setup_db()
    if len(sys.argv) > 1:
        admin(sys.argv[1:])
    else:
        from waitress import serve   # a real web server, safer than Flask's test server
        print(f"Typing Showdown running on port {PORT}")
        serve(app, host="0.0.0.0", port=PORT, threads=8)
