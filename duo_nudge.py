#!/usr/bin/env python3
"""
duo_nudge.py - push a "you have X minutes of Duolingo left today" nudge to your phone.

How it works
  1. Reads your user id from your Duolingo JWT (the token's "sub" claim).
  2. Calls Duolingo's unofficial xp_summaries endpoint for today (plus a tiny call for your
     streak length, which is optional and skipped if it fails).
  3. Converts today's lesson time (totalSessionTime, seconds) to minutes.
  4. If it's one of your nudge hours and you're under goal, sends a push via ntfy.

What the notification looks like (iPhone lock screen, ntfy app)
  🦉 37 min to go · 23 of 60 done
  6 lessons so far · 🔥 47-day streak
  About 10 more lessons · 4 h 36 min left today.

  - The emoji is part of the title, and no emoji tags are sent: ntfy prepends an emoji for
    each matching tag, so the old title + "owl" tag showed two owls.
  - Each day's nudges share one sequence id, so on clients that support it a new nudge
    replaces the previous one instead of stacking five stale ones.
  - Tapping opens Duolingo; the "Dashboard" button opens the live dashboard.

Config (environment variables)
  DUO_JWT            required  your Duolingo jwt_token cookie (treat like a password!)
  NTFY_TOPIC         required  your private ntfy topic name (make it long and random)
  DUO_GOAL_MINUTES   optional  daily goal in minutes            (default 60)
  DUO_NUDGE_HOURS    optional  local hours to nudge, 0-23        (default 12,16,19,21,22)
  DUO_TZ             optional  IANA time zone                    (default Europe/Zurich)
  NTFY_SERVER        optional  ntfy server                       (default https://ntfy.sh)
  DUO_DASHBOARD_URL  optional  link for the "Dashboard" button   (default the Pages URL)
  DUO_MOCK_JSON      optional  path to a saved API response, for offline testing

Flags
  --dry-run   print the notification instead of sending it
  --force     ignore the nudge-hour schedule (handy for testing)
  --debug     print the raw API response (to check field names)
"""
import argparse
import base64
import json
import math
import os
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


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
        "dashboard": env("DUO_DASHBOARD_URL", "https://antonemaes.github.io/duotracker/"),
        "mock": env("DUO_MOCK_JSON"),
    }


# ---------- Duolingo ----------

def user_id_from_jwt(jwt):
    """A JWT is header.payload.signature; the payload is base64url JSON with 'sub' = user id."""
    payload = jwt.split(".")[1]
    payload += "=" * (-len(payload) % 4)  # restore stripped padding
    return json.loads(base64.urlsafe_b64decode(payload))["sub"]


def api_get(cfg, url, timeout=20):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {cfg['jwt']}",
        "User-Agent": UA,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            sys.exit("Duolingo rejected the token (401/403). Grab a fresh jwt_token cookie.")
        raise


def fetch_summaries(cfg, today):
    if cfg["mock"]:
        with open(cfg["mock"]) as f:
            return json.load(f)
    uid = user_id_from_jwt(cfg["jwt"])
    return api_get(cfg, f"{API}/users/{uid}/xp_summaries"
                        f"?startDate={today}&endDate={today}&timezone={cfg['tz']}")


def fetch_streak(cfg):
    """Current streak length (a ~100-byte call). Optional: None on any failure."""
    if cfg["mock"]:
        return None
    try:
        uid = user_id_from_jwt(cfg["jwt"])
        return api_get(cfg, f"{API}/users/{uid}?fields=streak", timeout=10).get("streak")
    except Exception:  # the nudge must never fail because of the streak
        return None


def summary_date(s, tz):
    """'date' is epoch seconds at UTC midnight of the (local) day; accept ISO strings too."""
    d = s.get("date")
    if isinstance(d, (int, float)):
        return datetime.fromtimestamp(d, ZoneInfo("UTC")).date()
    if isinstance(d, str):
        return date.fromisoformat(d[:10])
    return None


def today_summary(data, today, tz):
    """(minutes, lessons, streak_extended) for today."""
    todays = [s for s in data.get("summaries", []) if summary_date(s, tz) == today]
    if not todays:
        return 0.0, 0, False  # no activity yet today
    s = todays[0]
    if "totalSessionTime" not in s:
        sys.exit("No 'totalSessionTime' in the response - run with --debug and check the field names.\n"
                 f"Fields seen: {sorted(s)}")
    return ((s.get("totalSessionTime") or 0) / 60, s.get("numSessions") or 0,
            bool(s.get("streakExtended") or s.get("frozen")))


def minutes_today(data, today, tz):
    """(minutes, lessons) for today - kept for callers of the old API."""
    done, sessions, _ = today_summary(data, today, tz)
    return done, sessions


# ---------- message ----------

def fmt_hours(hours):
    """4.6 -> '4 h 36 min', 0.75 -> '45 min'."""
    m = max(0, round(hours * 60))
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def build_message(done, sessions, goal, now, last_hour, streak=None, extended=False):
    """Return (title, body, priority) for the ntfy push.

    Four states, most urgent first: last call, behind pace, not started, on pace.
    The title carries the one number that matters (minutes left); the body says
    what's done and what to do next. Plain text: ntfy's iOS app doesn't render Markdown.
    """
    left = max(0, round(goal - done))
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), now.tzinfo)
    hours_left = (midnight - now).total_seconds() / 3600
    hl = fmt_hours(hours_left)
    done_r = round(done)
    lessons = f"{sessions} lesson{'' if sessions == 1 else 's'}"
    streak_txt = f"🔥 {streak}-day streak" if streak else None
    # "about N more lessons", from today's own minutes-per-lesson
    more = math.ceil(left / (done / sessions)) if sessions and done > 0 else None
    more_txt = f"About {more} more lesson{'' if more == 1 else 's'}" if more else None

    if now.hour >= last_hour:
        title = f"🚨 Last call: {left} min to go"
        lines = [f"{done_r} of {goal} min done · {hl} left today."]
        if streak and not extended:
            lines.append(f"Open Duolingo now to keep your {streak}-day streak.")
        else:
            lines.append("One focused session now gets you there.")
        priority = "high"
    elif left > hours_left * 60 * 0.5:
        title = f"⚠️ Behind pace: {left} min to go"
        lines = [f"{done_r} of {goal} min · {lessons} so far.",
                 f"Only {hl} left today. " + (f"{more_txt} - start now." if more_txt else "Start a lesson now.")]
        priority = "high"
    elif sessions == 0:
        title = f"🦉 Time for today's {goal} min"
        lines = ["No lessons yet today." + (f" {streak_txt} on the line." if streak_txt else "")]
        lines.append(f"{hl} left. A 15-minute session now makes it easy."
                     if hours_left >= 4 else f"{hl} left. Start now.")
        priority = "default"
    else:
        title = f"🦉 {left} min to go · {done_r} of {goal} done"
        lines = [f"{lessons} so far" + (f" · {streak_txt}" if streak_txt else ""),
                 (f"{more_txt} · " if more_txt else "") + f"{hl} left today."]
        priority = "default"
    return title, "\n".join(lines), priority


def send_ntfy(cfg, title, body, priority, day):
    if not cfg["topic"]:
        sys.exit("Missing NTFY_TOPIC")
    headers = {
        "Title": title.encode("utf-8"),  # ntfy accepts UTF-8 headers
        "Priority": priority,
        "Click": "duolingo://",          # tapping the notification opens Duolingo
        # One id per day: where the client supports it, the newest nudge replaces the last.
        "X-Sequence-ID": f"duo-nudge-{day}",
        "Actions": f"view, Open Duolingo, duolingo://, clear=true; view, Dashboard, {cfg['dashboard']}",
    }
    req = urllib.request.Request(f"{cfg['server']}/{cfg['topic']}", data=body.encode("utf-8"),
                                 headers=headers, method="POST")
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

    done, sessions, extended = today_summary(data, today, cfg["tz"])
    print(f"{today}: {done:.1f} / {cfg['goal']} min, {sessions} lessons")

    if done >= cfg["goal"]:
        print("Goal met — staying quiet. 🎉")
        return

    streak = fetch_streak(cfg)
    title, body, priority = build_message(done, sessions, cfg["goal"], now, cfg["hours"][-1],
                                          streak=streak, extended=extended)
    if args.dry_run:
        print(f"\n[{priority}] {title}\n{body}")
    else:
        send_ntfy(cfg, title, body, priority, today)
        print("Nudge sent.")


if __name__ == "__main__":
    main()
