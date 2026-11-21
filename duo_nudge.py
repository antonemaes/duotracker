#!/usr/bin/env python3
"""
duo_nudge.py - push a "you have X minutes of Duolingo left today" nudge to your phone.

How it works
  1. Reads your user id from your Duolingo JWT (the token's "sub" claim).
  2. Calls Duolingo's unofficial xp_summaries endpoint for today.
  3. Converts today's lesson time (totalSessionTime, seconds) to minutes.
  4. If it's one of your nudge hours and you're under goal, sends a push via ntfy.

Config (environment variables, or a .env file next to this script - see .env.example)
  DUO_JWT            required  your Duolingo jwt_token cookie (treat like a password!)
  NTFY_TOPIC         required  your private ntfy topic name (make it long and random)
  DUO_GOAL_MINUTES   optional  daily goal in minutes            (default 60)
  DUO_NUDGE_HOURS    optional  local hours to nudge, 0-23        (default 12,16,19,21,22)
  DUO_TZ             optional  IANA time zone                    (default Europe/Zurich)
  NTFY_SERVER        optional  ntfy server                       (default https://ntfy.sh)
  DUO_MOCK_JSON      optional  path to a saved API response, for offline testing

Flags
  --dry-run   print the notification instead of sending it
  --force     ignore the nudge-hour schedule (handy for testing)
  --debug     print the raw API response (to check field names)
"""
import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Load .env from the script's directory; real environment variables win (e.g. GitHub Actions secrets).
load_dotenv(Path(__file__).resolve().parent / ".env")

# Windows consoles default to a legacy code page that can't print emoji; the nudge body is UTF-8.
if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API = "https://www.duolingo.com/2017-06-30"
# Duolingo tends to reject requests that don't look like a browser.
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


# ---------- config ----------

def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"Missing required environment variable: {name}")
    return value


def load_config():
    return {
        "jwt": env("DUO_JWT", required=not os.environ.get("DUO_MOCK_JSON")),
        "topic": env("NTFY_TOPIC"),
        "server": env("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
        "goal": int(env("DUO_GOAL_MINUTES", "60")),
        "hours": sorted(int(h) for h in env("DUO_NUDGE_HOURS", "12,16,19,21,22").split(",")),
        "tz": ZoneInfo(env("DUO_TZ", "Europe/Zurich")),
        "mock": env("DUO_MOCK_JSON"),
    }


# ---------- Duolingo ----------

def user_id_from_jwt(jwt):
    """A JWT is header.payload.signature; the payload is base64url JSON with 'sub' = user id."""
    payload = jwt.split(".")[1]
    payload += "=" * (-len(payload) % 4)  # restore stripped padding
    return json.loads(base64.urlsafe_b64decode(payload))["sub"]


def fetch_summaries(cfg, today):
    if cfg["mock"]:
        with open(cfg["mock"]) as f:
            return json.load(f)
    uid = user_id_from_jwt(cfg["jwt"])
    tz_name = str(cfg["tz"])
    url = (f"{API}/users/{uid}/xp_summaries"
           f"?startDate={today}&endDate={today}&timezone={tz_name}")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {cfg['jwt']}",
        "User-Agent": UA,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            sys.exit("Duolingo rejected the token (401/403). Grab a fresh jwt_token cookie.")
        raise


def summary_date(s, tz):
    """'date' is epoch seconds at UTC midnight of the (local) day; accept ISO strings too."""
    d = s.get("date")
    if isinstance(d, (int, float)):
        return datetime.fromtimestamp(d, ZoneInfo("UTC")).date()
    if isinstance(d, str):
        return date.fromisoformat(d[:10])
    return None


def minutes_today(data, today, tz):
    summaries = data.get("summaries", [])
    todays = [s for s in summaries if summary_date(s, tz) == today]
    if not todays:
        return 0.0, 0  # no activity yet today
    s = todays[0]
    if "totalSessionTime" not in s:
        sys.exit("No 'totalSessionTime' in the response - run with --debug and check the field names.\n"
                 f"Fields seen: {sorted(s)}")
    return (s.get("totalSessionTime") or 0) / 60, s.get("numSessions") or 0


# ---------- message ----------

def build_message(done, sessions, goal, now, last_hour):
    left = max(0, round(goal - done))
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), now.tzinfo)
    hours_left = (midnight - now).total_seconds() / 3600
    pct = min(100, round(100 * done / goal)) if goal else 100
    bar = "▓" * (pct // 10) + "░" * (10 - pct // 10)

    title = f"🦉 {left} min of Duolingo left"
    body = (f"{bar} {pct}%\n"
            f"Done today: {round(done)} / {goal} min ({sessions} lessons)\n"
            f"{hours_left:.1f} h until midnight")
    if now.hour >= last_hour:
        priority, tags = "high", "rotating_light"
        body += "\nLast call — do it now!"
    elif left > hours_left * 60 * 0.5:
        priority, tags = "high", "warning"
        body += "\nYou're behind pace."
    else:
        priority, tags = "default", "owl"
    return title, body, priority, tags


def send_ntfy(cfg, title, body, priority, tags):
    if not cfg["topic"]:
        sys.exit("Missing NTFY_TOPIC")
    req = urllib.request.Request(
        f"{cfg['server']}/{cfg['topic']}",
        data=body.encode("utf-8"),
        headers={
            "Title": title.encode("utf-8"),  # ntfy accepts UTF-8 headers
            "Priority": priority,
            "Tags": tags,
            "Click": "duolingo://",          # tapping the notification opens Duolingo
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    now = datetime.now(cfg["tz"])
    today = now.date()

    if not args.force and now.hour not in cfg["hours"]:
        print(f"{now:%H:%M} is not a nudge hour {cfg['hours']}; exiting.")
        return

    data = fetch_summaries(cfg, today)
    if args.debug:
        print(json.dumps(data, indent=2))

    done, sessions = minutes_today(data, today, cfg["tz"])
    print(f"{today}: {done:.1f} / {cfg['goal']} min, {sessions} lessons")

    if done >= cfg["goal"]:
        print("Goal met — staying quiet. 🎉")
        return

    title, body, priority, tags = build_message(done, sessions, cfg["goal"], now, cfg["hours"][-1])
    if args.dry_run:
        print(f"\n[{priority}] {title}\n{body}")
    else:
        send_ntfy(cfg, title, body, priority, tags)
        print("Nudge sent.")


if __name__ == "__main__":
    main()
