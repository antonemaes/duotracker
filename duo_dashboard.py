#!/usr/bin/env python3
"""
duo_dashboard.py - build the Duotracker dashboard: a static page plus a small data.json,
written to a folder that GitHub Pages serves (default ./_site).

How it works
  1. xp_summaries for the history window (default 365 days): lessons, minutes and XP per day.
  2. users/{id} with a NESTED FIELD FILTER: only streak, total XP and the course path's
     section/unit/level counters. The unfiltered currentCourse object is ~9 MB; filtered and
     gzipped it is ~20 KB, which is what makes a 5-minute refresh cheap.
  3. Deutsche Welle (learngerman.dw.com), if DW_EMAIL and DW_PASSWORD are set:
     - the lesson list of each tracked course, from DW's public GraphQL API (no login);
     - your progress for those lessons: sign in, one POST for all lesson ids, sign out.
     A lesson counts as done when every exercise in it is answered (DW's own tick), on the
     day DW last saved it. Any DW failure only skips the DW numbers, never the dashboard.
  4. Writes OUT_DIR/data.json (everything the page shows, ~10 KB) and OUT_DIR/index.html
     (web/index.html with the same data inlined, so the first paint needs no extra request),
     plus the static assets in web/ (icons, manifest). The open page re-fetches data.json
     every minute, so it updates without a reload.

Sections are numbered like the Duolingo app: Section 1 is the API's index 0. Unit reviews are
left out of lesson counts (Duolingo marks them passed when a unit is finished, done or not).

Config (environment variables)
  DUO_JWT                 required (unless mocked)  Duolingo jwt_token cookie
  DUO_TZ                  optional  IANA time zone                      (default Europe/Zurich)
  DUO_HISTORY_DAYS        optional  days of history to fetch            (default 365)
  DUO_CHART_START         optional  ISO date the weekly/total charts start (default 2026-10-01)
  DUO_DAILY_TARGET        optional  daily lessons target                (default 25)
  DUO_WEEKLY_TARGET       optional  weekly lessons target               (default 175 = 25/day)
  DUO_PROJECTION_GROUPS   optional  finish-date cards: name=sections;... (default B1=5,6;B2=7,8)
  DW_EMAIL, DW_PASSWORD   optional  your learngerman.dw.com sign-in; without them DW is left out
  DW_COURSES              optional  DW course ids to track, main one first
                                    (default: the B1 courses Nicos Weg, Fit für den Job,
                                    Berufe entdecken and Jojo sucht das Glück)
  DW_LEVEL                optional  label for the tracked courses       (default B1)
  DW_DAILY_TARGET         optional  DW lessons per day                  (default 4)
  DW_MOCK_COURSES_JSON    optional  saved GraphQL course response, for offline runs
  DW_MOCK_PROGRESS_JSON   optional  saved lesson-progress response, for offline runs
  DASHBOARD_OUT_DIR       optional  output folder                       (default _site)
  DUO_REPO                optional  GitHub "owner/repo", for links      (default antonemaes/duotracker)
  DUO_PAGE_URL            optional  public URL of the page              (default https://<owner>.github.io/<repo>/)
  DUO_MOCK_JSON           optional  saved xp_summaries response, for offline runs
  DUO_MOCK_COURSE_JSON    optional  saved users/{id} (course) response, for offline runs

The Duolingo endpoints and DW's progress endpoint are unofficial and undocumented; any of
them could change or break at any time.

Flags
  --out-dir PATH     output folder (overrides DASHBOARD_OUT_DIR)
  --days N           history window in days (overrides DUO_HISTORY_DAYS)
  --chart-start DATE ISO date the charts start at (overrides DUO_CHART_START)
  --daily-target N   daily lessons target (overrides DUO_DAILY_TARGET)
  --weekly-target N  weekly lessons target (overrides DUO_WEEKLY_TARGET)
  --mock PATH        offline mode: saved xp_summaries response
  --mock-course PATH offline mode: saved users/{id} course response
  --debug            print the start of the raw API responses (field-name checking)
"""
import argparse
import base64
import gzip
import http.cookiejar
import json
import math
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API = "https://www.duolingo.com/2017-06-30"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
WEB_DIR = Path(__file__).resolve().parent / "web"

# Only the fields the dashboard uses. Duolingo's API accepts nested field filters
# (field{subfield,...}); without the filter currentCourse alone is ~9 MB.
COURSE_FIELDS = (
    "streak,streakData{currentStreak,longestStreak},totalXp,learningLanguage,fromLanguage,"
    "currentCourse{title,pathSectioned{index,type,units{unitIndex,teachingObjective,cefrLevel,"
    "levels{finishedSessions,totalSessions,state,type}}}}"
)

DW_SITE = "https://learngerman.dw.com"
DW_GRAPHQL = "https://webapi.dw.com/graphql"
# Nicos Weg B1, Fit für den Job, Berufe entdecken, Jojo sucht das Glück: DW's B1 courses.
DW_DEFAULT_COURSES = "36519718,73052672,39902336,53437698"
# Per course: name, link and the lessons in order (chapter, title, link). Nothing else.
DW_COURSE_FRAGMENT = ("fragment C on Course { name namedUrl contentLinks(targetTypes: LESSON) "
                      "{ id groupName target { ... on Lesson { shortTitle namedUrl } } } }")

DONE_STATES = {"passed", "legendary"}
OPTIONAL_LEVEL_TYPES = {"unit_review"}   # auto-passed when its unit is finished

LANGUAGES = {
    "ar": "Arabic", "ca": "Catalan", "cs": "Czech", "cy": "Welsh", "da": "Danish", "de": "German",
    "el": "Greek", "en": "English", "eo": "Esperanto", "es": "Spanish", "fi": "Finnish",
    "fr": "French", "ga": "Irish", "he": "Hebrew", "hi": "Hindi", "hu": "Hungarian",
    "id": "Indonesian", "it": "Italian", "ja": "Japanese", "ko": "Korean", "la": "Latin",
    "nl": "Dutch", "nb": "Norwegian", "no": "Norwegian", "pl": "Polish", "pt": "Portuguese",
    "ro": "Romanian", "ru": "Russian", "sv": "Swedish", "sw": "Swahili", "tr": "Turkish",
    "uk": "Ukrainian", "vi": "Vietnamese", "zh": "Chinese", "zs": "Chinese",
}


# ---------- config ----------

def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"Missing required environment variable: {name}")
    return value


def load_config(args):
    mock = args.mock or env("DUO_MOCK_JSON")
    mock_course = args.mock_course or env("DUO_MOCK_COURSE_JSON")
    repo = env("DUO_REPO", "antonemaes/duotracker")
    owner, _, name = repo.partition("/")
    return {
        "jwt": env("DUO_JWT", required=not (mock and mock_course)),
        "tz": ZoneInfo(env("DUO_TZ", "Europe/Zurich")),
        "mock": mock,
        "mock_course": mock_course,
        "days": args.days or int(env("DUO_HISTORY_DAYS", "365")),
        "chart_start": date.fromisoformat(args.chart_start or env("DUO_CHART_START", "2026-10-01")),
        "daily_target": args.daily_target or int(env("DUO_DAILY_TARGET", "25")),
        "weekly_target": args.weekly_target or int(env("DUO_WEEKLY_TARGET", "175")),
        "proj_groups": parse_groups(env("DUO_PROJECTION_GROUPS", "B1=5,6;B2=7,8")),
        "dw_email": env("DW_EMAIL"),
        "dw_password": env("DW_PASSWORD"),
        "dw_courses": [int(x) for x in env("DW_COURSES", DW_DEFAULT_COURSES).split(",") if x.strip()],
        "dw_level": env("DW_LEVEL", "B1"),
        "dw_daily_target": int(env("DW_DAILY_TARGET", "4")),
        "dw_mock_courses": env("DW_MOCK_COURSES_JSON"),
        "dw_mock_progress": env("DW_MOCK_PROGRESS_JSON"),
        "out_dir": Path(args.out_dir or env("DASHBOARD_OUT_DIR", "_site")),
        "repo": repo,
        "page_url": env("DUO_PAGE_URL", f"https://{owner.lower()}.github.io/{name}/"),
    }


# ---------- Duolingo ----------

def user_id_from_jwt(jwt):
    payload = jwt.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))["sub"]


def api_get(cfg, url, timeout, label):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {cfg['jwt']}",
        "User-Agent": UA,
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            wire = len(raw)
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            sys.exit("Duolingo rejected the token (401/403). Grab a fresh jwt_token cookie.")
        raise
    # Log sizes, never the URL: it holds the user id, and Actions logs of a public repo are public.
    print(f"{label}: {wire:,} bytes on the wire, {len(raw):,} bytes of JSON")
    if wire > 2_000_000:
        print(f"::warning::{label} returned more than 2 MB - the nested field filter may no longer be honored.")
    return json.loads(raw)


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fetch_range(cfg, start, end):
    """xp_summaries for the whole history window (one small call)."""
    if cfg["mock"]:
        return load_json(cfg["mock"])
    uid = user_id_from_jwt(cfg["jwt"])
    url = (f"{API}/users/{uid}/xp_summaries"
           f"?startDate={start}&endDate={end}&timezone={cfg['tz']}")
    return api_get(cfg, url, timeout=30, label="xp_summaries")


def fetch_course(cfg):
    """users/{id} filtered down to streak, XP and the path's counters (~20 KB gzipped)."""
    if cfg["mock_course"]:
        return load_json(cfg["mock_course"])
    uid = user_id_from_jwt(cfg["jwt"])
    fields = urllib.parse.quote(COURSE_FIELDS, safe=",")
    return api_get(cfg, f"{API}/users/{uid}?fields={fields}", timeout=60, label="users (filtered course)")


def summary_date(s):
    """'date' is epoch seconds at UTC midnight of the (local) day; accept ISO strings too."""
    d = s.get("date")
    if isinstance(d, (int, float)):
        return datetime.fromtimestamp(d, ZoneInfo("UTC")).date()
    if isinstance(d, str):
        return date.fromisoformat(d[:10])
    return None


# ---------- course path (live section/unit/level progress) ----------

def _steps_of(unit):
    return unit.get("levels") or unit.get("steps") or []


def _cefr_label(value):
    if not value:
        return None
    value = str(value)
    return value.title() if value.isalpha() and len(value) > 2 else value.upper()


def parse_course(data):
    """Per-section lesson totals from currentCourse.pathSectioned.

    - Section numbers follow the app (API index 0 -> Section 1).
    - Non-learning sections (the trailing 'daily_refresh' pseudo-section) are skipped.
    - A level counts as done when its state is passed/legendary, even if Duolingo left
      finishedSessions < totalSessions (e.g. levels you skipped with a jump test).
    - Unit reviews are excluded: they are auto-passed when a unit is finished.
    - The current unit is the one holding the 'active' level (fallback: first unfinished).
    Tolerates older shapes: 'steps' instead of 'levels', no state/type/cefr fields.
    """
    course = (data or {}).get("currentCourse") or {}
    sections, current, first_open = [], None, None
    for pos, sec in enumerate(course.get("pathSectioned") or []):
        if sec.get("type") not in (None, "learning"):
            continue
        idx = sec.get("index")
        num = idx + 1 if isinstance(idx, int) else pos + 1
        units = sec.get("units") or ([sec] if _steps_of(sec) else [])
        total = done = units_done = 0
        cefr = None
        for upos, unit in enumerate(units):
            cefr = cefr or unit.get("cefrLevel")
            u_total = u_done = 0
            active = False
            steps = _steps_of(unit)
            if steps:
                for st in steps:
                    if st.get("type") in OPTIONAL_LEVEL_TYPES:
                        continue
                    t = int(st.get("totalSessions") or 0)
                    if st.get("state") in DONE_STATES:
                        f = t
                    else:
                        f = min(int(st.get("finishedSessions") or 0), t)
                    u_total += t
                    u_done += f
                    active = active or st.get("state") == "active"
            else:
                u_total = int(unit.get("totalSessions") or 0)
                u_done = min(int(unit.get("finishedSessions") or 0), u_total)
            total += u_total
            done += u_done
            if u_total and u_done >= u_total:
                units_done += 1
            here = {
                "section": num,
                "unit": upos + 1,
                "units": len(units),
                "title": unit.get("teachingObjective") or unit.get("title") or unit.get("name"),
                "cefr": _cefr_label(unit.get("cefrLevel")),
            }
            if active and current is None:
                current = here
            if first_open is None and u_total > u_done:
                first_open = here
        sections.append({
            "n": num,
            "cefr": _cefr_label(cefr),
            "units": len(units),
            "unitsDone": units_done,
            "total": total,
            "done": min(done, total),
        })
    current = current or first_open
    for s in sections:
        s["active"] = bool(current and current["section"] == s["n"])
    return {
        "title": course.get("title") or None,
        "sections": sections,
        "total": sum(s["total"] for s in sections),
        "done": sum(s["done"] for s in sections),
        "current": current,
    }


def parse_groups(spec):
    """'B1=5,6;B2=7,8' -> [{'name': 'B1', 'sections': [5, 6]}, ...].
    A part without '=' (e.g. the old '5,6' format) gives one group per section."""
    groups = []
    for part in (spec or "").split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            name, nums = part.split("=", 1)
            groups.append({"name": name.strip(),
                           "sections": sorted(int(x) for x in nums.split(",") if x.strip())})
        else:
            groups += [{"name": f"Section {int(x)}", "sections": [int(x)]}
                       for x in part.split(",") if x.strip()]
    return groups


def _eta(through, today, rate, daily_target):
    """Finish date for `through` remaining lessons at `rate`/day, plus the what-if date at
    the daily target pace."""
    if through <= 0:
        return {"finished": True}
    out = {}
    if rate > 0:
        out["days"] = math.ceil(through / rate)
        out["date"] = (today + timedelta(days=out["days"])).isoformat()
    if daily_target > 0:
        out["targetDays"] = math.ceil(through / daily_target)
        out["targetDate"] = (today + timedelta(days=out["targetDays"])).isoformat()
    return out


def project_groups(sections, today, rate, daily_target, groups):
    """Finish-date projections per group of sections (e.g. a CEFR level). Sections are
    completed in order, so a group is done when its last section is: every unfinished
    lesson up to and including that section counts ('through')."""
    by_num = {s["n"]: s for s in sections}
    out = []
    for g in groups:
        secs = [by_num[n] for n in g["sections"] if n in by_num]
        name = g["name"]
        p = {"name": name, "sections": g["sections"],
             "label": f"{name} level" if re.fullmatch(r"[ABC][12]", name) else name}
        if not secs:
            p["missing"] = True
            out.append(p)
            continue
        last = max(s["n"] for s in secs)
        p["total"] = sum(s["total"] for s in secs)
        p["done"] = sum(s["done"] for s in secs)
        p["remaining"] = p["total"] - p["done"]
        p["through"] = sum(x["total"] - x["done"] for x in sections if x["n"] <= last)
        p.update(_eta(p["through"], today, rate, daily_target))
        out.append(p)
    return out


def section_etas(sections, today, rate):
    """Add an 'eta' (ISO date at `rate`/day) to every unfinished section, in place."""
    through = 0
    for s in sections:
        through += s["total"] - s["done"]
        if s["total"] > s["done"] and rate > 0:
            s["eta"] = (today + timedelta(days=math.ceil(through / rate))).isoformat()
    return sections


# ---------- Deutsche Welle ----------

class DWError(Exception):
    pass


def _dw_request(url, body=None):
    return urllib.request.Request(
        url, data=None if body is None else json.dumps(body).encode("utf-8"),
        headers={"User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json",
                 "Accept-Encoding": "gzip"},
        method="GET" if body is None else "POST")


def _dw_read(opener, req, label, timeout=30):
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
        wire = len(raw)
        if resp.headers.get("Content-Encoding") == "gzip":
            raw = gzip.decompress(raw)
    print(f"{label}: {wire:,} bytes on the wire, {len(raw):,} bytes of JSON")
    return json.loads(raw) if raw.strip() else None


def fetch_dw_courses(cfg):
    """Lesson lists of the tracked courses: one public GraphQL call (~7 KB gzipped for four)."""
    if cfg["dw_mock_courses"]:
        return load_json(cfg["dw_mock_courses"])
    parts = " ".join(f"c{cid}: content(id: {cid}, lang: GERMAN) {{ ...C }}" for cid in cfg["dw_courses"])
    data = _dw_read(urllib.request.build_opener(),
                    _dw_request(DW_GRAPHQL, {"query": f"{{ {parts} }} {DW_COURSE_FRAGMENT}"}),
                    "DW courses (GraphQL)")
    if not data or data.get("errors"):
        raise DWError(f"DW's GraphQL API returned errors: {(data or {}).get('errors')}")
    return data


def fetch_dw_progress(cfg, lesson_ids):
    """Your progress records for `lesson_ids`. DW keeps progress per account behind a session
    cookie, so: sign in, one POST for every lesson at once, sign out."""
    if cfg["dw_mock_progress"]:
        return load_json(cfg["dw_mock_progress"])
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    login = _dw_request(f"{DW_SITE}/profiles/login",
                        {"username": cfg["dw_email"], "password": cfg["dw_password"]})
    try:
        opener.open(login, timeout=20).read()
    except urllib.error.HTTPError as e:
        try:
            reason = json.loads(e.read() or b"{}").get("error")
        except (ValueError, AttributeError):
            reason = None
        raise DWError(f"DW sign-in failed (HTTP {e.code}{': ' + str(reason) if reason else ''})") from None
    try:
        return _dw_read(opener, _dw_request(f"{DW_SITE}/profiles/protected/lesson/progress",
                                            {"ids": lesson_ids}), "DW lesson progress") or []
    finally:
        try:
            opener.open(_dw_request(f"{DW_SITE}/profiles/public/logout"), timeout=10).read()
        except Exception:  # signing out is a courtesy; the numbers are already in
            pass


def dw_lesson_ids(courses_raw):
    blocks = (courses_raw or {}).get("data") or {}
    return [link["id"] for c in blocks.values() if c
            for link in c.get("contentLinks") or [] if link.get("id") is not None]


def dw_lesson_done(p):
    """DW ticks a lesson off once every exercise in it is answered; the score doesn't matter."""
    if not p:
        return False
    total = int(p.get("allExerciseCount") or 0)
    return total > 0 and int(p.get("doneExerciseCount") or 0) >= total


def _dw_day(iso, tz):
    """'2026-11-23T13:29:11.564Z' -> the local date it was saved on."""
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone(tz).date()
    except ValueError:
        return None


def _dw_url(path):
    return DW_SITE + urllib.parse.quote(path, safe="/-_.~") if path else None


def parse_dw(courses_raw, progress_raw, course_ids, today, tz, daily_target, level):
    """DW numbers for data.json.

    Lessons are counted on the day DW last saved them, so re-doing an exercise in an old
    lesson moves that lesson to today. Courses come back in `course_ids` order; only the
    first (main) one and the ones you've started are listed, to keep the card short.
    """
    blocks = (courses_raw or {}).get("data") or {}
    prog = {p["cosLessonId"]: p for p in progress_raw or []
            if isinstance(p, dict) and p.get("cosLessonId") is not None}
    per_day = Counter()
    courses, done_all, total_all = [], 0, 0
    for pos, cid in enumerate(course_ids):
        c = blocks.get(f"c{cid}")
        if not c:
            continue
        total = done = started = 0
        nxt = last = None
        for link in c.get("contentLinks") or []:
            target = link.get("target") or {}
            p = prog.get(link.get("id"))
            total += 1
            if dw_lesson_done(p):
                done += 1
                d = _dw_day(p.get("lastModifiedDate"), tz)
                if d:
                    per_day[d] += 1
                    last = max(last, d) if last else d
                continue
            ex_done = int((p or {}).get("doneExerciseCount") or 0)
            if ex_done:
                started += 1
            if nxt is None:
                nxt = {"title": (target.get("shortTitle") or "").strip() or None,
                       "chapter": (link.get("groupName") or "").strip() or None,
                       "url": _dw_url(target.get("namedUrl"))}
                if ex_done:
                    nxt["exercises"] = [ex_done, int(p.get("allExerciseCount") or 0)]
        done_all += done
        total_all += total
        if pos == 0 or done or started:
            course = {"id": cid, "name": (c.get("name") or "").strip(), "url": _dw_url(c.get("namedUrl")),
                      "total": total, "done": done, "inProgress": started, "next": nxt,
                      "lastDone": last.isoformat() if last else None}
            course.update(_eta(total - done, today, 0, daily_target))
            courses.append(course)
    week_start = today - timedelta(days=today.weekday())
    return {
        "level": level,
        "today": per_day[today],
        "week": sum(n for d, n in per_day.items() if week_start <= d <= today),
        "rate7": round(sum(per_day[today - timedelta(days=k)] for k in range(1, 8)) / 7, 2),
        "last7": [per_day[today - timedelta(days=k)] for k in range(6, -1, -1)],
        "done": done_all,
        "total": total_all,
        "courses": courses,
    }


def load_dw(cfg, today):
    """data.json's 'dw' block: None when DW isn't set up, {'error': ...} when it failed."""
    if not ((cfg["dw_email"] and cfg["dw_password"]) or cfg["dw_mock_progress"]):
        print("DW: no DW_EMAIL/DW_PASSWORD, leaving Deutsche Welle out.")
        return None
    try:
        courses_raw = fetch_dw_courses(cfg)
        progress_raw = fetch_dw_progress(cfg, dw_lesson_ids(courses_raw))
        return parse_dw(courses_raw, progress_raw, cfg["dw_courses"], today, cfg["tz"],
                        cfg["dw_daily_target"], cfg["dw_level"])
    except (DWError, OSError, ValueError, KeyError, TypeError) as e:
        # Never the URL or the request: the message is safe for a public Actions log.
        msg = str(e) if isinstance(e, DWError) else f"{type(e).__name__}: {e}"
        print(f"::warning::Deutsche Welle progress skipped - {msg}")
        return {"error": msg, "level": cfg["dw_level"]}


# ---------- history ----------

def daily_series(data, start, end):
    """[{date, lessons, minutes, xp, extended}] for every day in [start, end]; gaps are zeros."""
    by_day = {}
    for s in data.get("summaries", []):
        d = summary_date(s)
        if d and start <= d <= end:
            by_day[d] = s
    days, d = [], start
    while d <= end:
        s = by_day.get(d, {})
        days.append({
            "date": d,
            "lessons": int(s.get("numSessions") or 0),
            "minutes": (s.get("totalSessionTime") or 0) / 60,
            "xp": int(s.get("gainedXp") or 0),
            "extended": bool(s.get("streakExtended") or s.get("frozen")),
        })
        d += timedelta(days=1)
    return days


def avg_lessons(days):
    return sum(d["lessons"] for d in days) / len(days) if days else 0.0


# ---------- data.json ----------

def build_data(cfg, now, xp_raw, course_raw, dw=None):
    """Everything the page shows. `dw` is load_dw()'s block (None leaves DW out)."""
    today = now.date()
    start = today - timedelta(days=cfg["days"] - 1)
    days = daily_series(xp_raw, start, today)
    full = days[:-1]                      # complete days only: today is still in progress
    rate7 = avg_lessons(full[-7:])
    rate_prev7 = avg_lessons(full[-14:-7])

    course = parse_course(course_raw)
    projections = project_groups(course["sections"], today, rate7,
                                 cfg["daily_target"], cfg["proj_groups"])
    section_etas(course["sections"], today, rate7)

    week_start = today - timedelta(days=today.weekday())
    t = days[-1]
    lang = (course_raw or {}).get("learningLanguage")
    from_lang = (course_raw or {}).get("fromLanguage")
    streak_data = (course_raw or {}).get("streakData") or {}
    longest = (streak_data.get("longestStreak") or {}).get("length")
    streak = (course_raw or {}).get("streak")

    return {
        "v": 1,
        "generatedAt": now.isoformat(timespec="seconds"),
        "tz": str(cfg["tz"]),
        "today": today.isoformat(),
        "repo": cfg["repo"],
        "course": {
            "language": course["title"] or LANGUAGES.get(lang, (lang or "Duolingo").upper()),
            "code": (lang or "").upper()[:2] or None,
            "from": LANGUAGES.get(from_lang, from_lang),
        },
        "targets": {
            "dailyLessons": cfg["daily_target"],
            "weeklyLessons": cfg["weekly_target"],
            "dwDailyLessons": cfg["dw_daily_target"],
        },
        "todayStats": {
            "lessons": t["lessons"],
            "minutes": round(t["minutes"]),
            "xp": t["xp"],
            "extended": t["extended"],
        },
        "streak": {"current": streak, "longest": longest},
        "totalXp": (course_raw or {}).get("totalXp"),
        "stats": {
            "week": sum(d["lessons"] for d in days if d["date"] >= week_start),
            "rate7": round(rate7, 2),
            "ratePrev7": round(rate_prev7, 2),
            "historyTotal": sum(d["lessons"] for d in days),
        },
        "chartStart": max(start, min(cfg["chart_start"], today)).isoformat(),
        "history": {
            "start": start.isoformat(),
            "lessons": [d["lessons"] for d in days],
            "minutes": [round(d["minutes"]) for d in days],
            "xp": [d["xp"] for d in days],
        },
        "path": {
            "total": course["total"],
            "done": course["done"],
            "current": course["current"],
            "sections": course["sections"],
        },
        "projections": projections,
        "dw": dw,
    }


# ---------- site ----------

def write_site(out_dir, data, page_url, web_dir=WEB_DIR):
    """Copy web/ into out_dir, inline the data into index.html, and write data.json."""
    template = (web_dir / "index.html").read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # The seed lives inside <script type="application/json">: never let data close the tag.
    seed = payload.replace("</", "<\\/").replace("<!--", "<\\!--")
    page = template.replace("__PAGE_URL__", page_url).replace("__DUO_SEED__", seed)

    out_dir.mkdir(parents=True, exist_ok=True)
    for f in web_dir.iterdir():
        if f.is_file() and f.name != "index.html":
            shutil.copy2(f, out_dir / f.name)
    (out_dir / "index.html").write_text(page, encoding="utf-8")
    (out_dir / "data.json").write_text(payload, encoding="utf-8")
    return out_dir / "index.html", out_dir / "data.json"


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out-dir", help="output folder (overrides DASHBOARD_OUT_DIR)")
    ap.add_argument("--days", type=int, help="history window in days (overrides DUO_HISTORY_DAYS)")
    ap.add_argument("--chart-start", help="ISO date the charts start at (overrides DUO_CHART_START)")
    ap.add_argument("--daily-target", type=int, help="daily lessons target (overrides DUO_DAILY_TARGET)")
    ap.add_argument("--weekly-target", type=int, help="weekly lessons target (overrides DUO_WEEKLY_TARGET)")
    ap.add_argument("--mock", help="saved xp_summaries response (offline)")
    ap.add_argument("--mock-course", help="saved users/{id} course response (offline)")
    ap.add_argument("--debug", action="store_true", help="print the raw API responses")
    args = ap.parse_args()

    cfg = load_config(args)
    now = datetime.now(cfg["tz"])
    today = now.date()
    start = today - timedelta(days=cfg["days"] - 1)

    xp_raw = fetch_range(cfg, start, today)
    course_raw = fetch_course(cfg)
    if args.debug:
        print(json.dumps(xp_raw, indent=2)[:3000])
        print(json.dumps(course_raw, indent=2)[:3000])

    dw = load_dw(cfg, today)
    data = build_data(cfg, now, xp_raw, course_raw, dw)
    page, data_file = write_site(cfg["out_dir"], data, cfg["page_url"])

    st, td, path = data["stats"], data["todayStats"], data["path"]
    print(f"Wrote {page} ({page.stat().st_size:,} bytes) and {data_file} ({data_file.stat().st_size:,} bytes)")
    print(f"{today}: {td['lessons']} lessons / {td['minutes']} min / {td['xp']} XP today; "
          f"7-day avg {st['rate7']:.2f}/day; streak {data['streak']['current']}")
    cur = path["current"]
    print(f"course: {path['done']:,}/{path['total']:,} path lessons done"
          + (f", now at Section {cur['section']} unit {cur['unit']}/{cur['units']}: {cur['title']}" if cur else ""))
    for p in data["projections"]:
        secs = ",".join(str(n) for n in p["sections"])
        if p.get("missing"):
            print(f"{p['label']} (sections {secs}): not in the current course")
        elif p.get("finished"):
            print(f"{p['label']} (sections {secs}): finished")
        else:
            print(f"{p['label']} (sections {secs}): {p['through']:,} lessons to go -> "
                  f"{p.get('date', 'n/a')} at {st['rate7']:.1f}/day, {p.get('targetDate')} at target pace")
    if dw and not dw.get("error"):
        print(f"DW {dw['level']}: {dw['today']} / {data['targets']['dwDailyLessons']} lessons today, "
              f"{dw['week']} this week, {dw['done']}/{dw['total']} done in the tracked courses")
        for c in dw["courses"]:
            print(f"  {c['name']}: {c['done']}/{c['total']} lessons"
                  + (f", {c['inProgress']} in progress" if c["inProgress"] else ""))


if __name__ == "__main__":
    main()
