#!/usr/bin/env python3
"""
duo_dashboard.py - generate a self-contained, mobile-friendly HTML dashboard of your
Duolingo lesson progress, with projected completion dates for sections 5 and 6.

How it works
  1. Fetches your xp_summaries for a date range (default: the last 365 days) - one small call.
     numSessions per day = lessons completed that day.
  2. Fetches users/{id}?fields=currentCourse - one BIG call (~9 MB). currentCourse.pathSectioned
     contains every section, unit and step with totalSessions/finishedSessions, so remaining
     lessons per section are read live (no hand-maintained config). Run this DAILY, never hourly.
  3. Builds weekly lesson totals (bars) and cumulative lessons (line).
  4. Projects cumulative lessons forward at the 7-day moving average until
     DUO_PROJECTION_END (default 2027-03-31, i.e. end of Q1 2027).
  5. Projects each section's completion date from its live remaining lessons divided by the
     7-day average. The cards go at the top of the page.

Config (environment variables)
  DUO_JWT                 required (unless mocked)  Duolingo jwt_token cookie
  DUO_TZ                  optional  IANA time zone                     (default Europe/Zurich)
  DUO_MOCK_JSON           optional  saved xp_summaries response, for offline testing
  DUO_MOCK_COURSE_JSON    optional  saved currentCourse response, for offline testing
  DUO_HISTORY_DAYS        optional  days of history to fetch           (default 365)
  DUO_CHART_START         optional  ISO date the graph's x-axis starts (default 2026-10-01)
  DUO_PROJECTION_END      optional  ISO date the projection runs to    (default 2027-03-31)
  DUO_PROJECTION_SECTIONS optional  comma-separated section numbers  (default 5,6)
  DASHBOARD_OUT           optional  output HTML file                   (default index.html)

Both endpoints are unofficial and undocumented; either could change or break at any time.

Flags
  --out PATH         output file (overrides DASHBOARD_OUT)
  --days N           history window in days (overrides DUO_HISTORY_DAYS)
  --chart-start DATE ISO date the graph starts at (overrides DUO_CHART_START)
  --mock PATH        offline mode: saved xp_summaries response
  --mock-course PATH offline mode: saved currentCourse response
  --debug            print the raw API responses (field-name checking)
"""
import argparse
import base64
import html
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

API = "https://www.duolingo.com/2017-06-30"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


# ---------- config ----------

def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"Missing required environment variable: {name}")
    return value


def load_config(args):
    mocked = os.environ.get("DUO_MOCK_JSON") or args.mock
    return {
        "jwt": env("DUO_JWT", required=not (mocked or os.environ.get("DUO_MOCK_COURSE_JSON") or args.mock_course)),
        "tz": ZoneInfo(env("DUO_TZ", "Europe/Zurich")),
        "mock": args.mock or env("DUO_MOCK_JSON"),
        "mock_course": args.mock_course or env("DUO_MOCK_COURSE_JSON"),
        "days": args.days or int(env("DUO_HISTORY_DAYS", "365")),
        "chart_start": date.fromisoformat(args.chart_start or env("DUO_CHART_START", "2026-10-01")),
        "proj_end": date.fromisoformat(env("DUO_PROJECTION_END", "2027-03-31")),
        "proj_sections": sorted(int(s) for s in env("DUO_PROJECTION_SECTIONS", "5,6").split(",")),
        "out": Path(args.out or env("DASHBOARD_OUT", "index.html")),
    }


# ---------- Duolingo ----------

def user_id_from_jwt(jwt):
    payload = jwt.split(".")[1]
    payload += "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))["sub"]


def api_get(cfg, url, timeout):
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


def fetch_range(cfg, start, end):
    """Small call: xp_summaries for the whole history range."""
    if cfg["mock"]:
        with open(cfg["mock"]) as f:
            return json.load(f)
    uid = user_id_from_jwt(cfg["jwt"])
    url = (f"{API}/users/{uid}/xp_summaries"
           f"?startDate={start}&endDate={end}&timezone={cfg['tz']}")
    return api_get(cfg, url, timeout=30)


def fetch_current_course(cfg):
    """BIG call (~9 MB): the full section/unit/step tree. Fetch daily at most."""
    if cfg["mock_course"]:
        with open(cfg["mock_course"]) as f:
            return json.load(f)
    uid = user_id_from_jwt(cfg["jwt"])
    return api_get(cfg, f"{API}/users/{uid}?fields=currentCourse", timeout=90)


def summary_date(s):
    """'date' is epoch seconds at UTC midnight of the (local) day; accept ISO strings too."""
    d = s.get("date")
    if isinstance(d, (int, float)):
        return datetime.fromtimestamp(d, ZoneInfo("UTC")).date()
    if isinstance(d, str):
        return date.fromisoformat(d[:10])
    return None


# ---------- course path (live section/unit/step progress) ----------

def _steps_of(unit):
    return unit.get("steps") or unit.get("levels") or []


def parse_course(data):
    """Extract per-section lesson totals from currentCourse.pathSectioned.

    Tolerates 'steps' or 'levels' under each unit, sections that carry steps directly,
    and missing title/index fields (falls back to position and generic labels).
    """
    course = (data or {}).get("currentCourse") or {}
    sections = []
    current_unit = None
    for i, sec in enumerate(course.get("pathSectioned") or [], start=1):
        num = sec.get("index") or sec.get("number") or sec.get("sectionIndex")
        if num is None:
            m = re.search(r"(\d+)", str(sec.get("title") or sec.get("name") or ""))
            num = int(m.group(1)) if m else i
        units = sec.get("units") or ([sec] if _steps_of(sec) else [])
        total = finished = 0
        for u in units:
            steps = _steps_of(u)
            u_total = u_finished = 0
            if steps:
                for st in steps:
                    t = int(st.get("totalSessions") or 0)
                    u_total += t
                    u_finished += min(int(st.get("finishedSessions") or 0), t)
            else:
                u_total = int(u.get("totalSessions") or 0)
                u_finished = min(int(u.get("finishedSessions") or 0), u_total)
            total += u_total
            finished += u_finished
            if current_unit is None and u_total > u_finished:
                label = u.get("title") or u.get("name") or u.get("label")
                if not label:
                    idx = u.get("index") or u.get("number") or u.get("id")
                    label = f"Unit {idx}" if idx is not None else "current unit"
                current_unit = f"Section {num}: {label}"
        sections.append({
            "num": int(num),
            "title": sec.get("title") or sec.get("name") or f"Section {num}",
            "total": total,
            "finished": min(finished, total),
            "remaining": max(0, total - finished),
        })
    return {
        "title": course.get("title") or "Duolingo",
        "sections": sections,
        "total": sum(s["total"] for s in sections),
        "finished": sum(s["finished"] for s in sections),
        "current_unit": current_unit,
    }


def project_sections(sections, today, ma7, wanted):
    """{section_num: (projected_end_date, remaining_in_section, days_needed, remaining_through_section)}
    assuming a constant ma7 lessons/day. Sections are completed in order."""
    out = {}
    if ma7 <= 0:
        return out
    by_num = {s["num"]: s for s in sections}
    for n in wanted:
        if n not in by_num:
            continue
        through = sum(s["remaining"] for s in sections if s["num"] <= n)
        if through <= 0:
            out[n] = (today, 0, 0, 0)
            continue
        days = math.ceil(through / ma7)
        out[n] = (today + timedelta(days=days), by_num[n]["remaining"], days, through)
    return out


# ---------- history data shaping ----------

def daily_series(data, start, end):
    """[(date, lessons, minutes)] for every day in [start, end]; missing days are zeros."""
    by_day = {}
    for s in data.get("summaries", []):
        d = summary_date(s)
        if d and start <= d <= end:
            by_day[d] = (int(s.get("numSessions") or 0), (s.get("totalSessionTime") or 0) / 60)
    days, d = [], start
    while d <= end:
        lessons, minutes = by_day.get(d, (0, 0.0))
        days.append((d, lessons, minutes))
        d += timedelta(days=1)
    return days


def weekly(days):
    """[(week_start_monday, lessons)] aggregated by week."""
    agg = {}
    for d, lessons, _ in days:
        ws = d - timedelta(days=d.weekday())
        agg[ws] = agg.get(ws, 0) + lessons
    return sorted(agg.items())


def cumulative(days):
    out, total = [], 0
    for d, lessons, _ in days:
        total += lessons
        out.append((d, total))
    return out


def moving_avg7(days):
    """Trailing 7-day moving average of lessons, including today."""
    return sum(l for _, l, _ in days[-7:]) / 7


# ---------- chart (inline SVG, no external deps) ----------

def render_chart(days, weeks, cum, proj, today, x_end, section_dates):
    W, H = 720, 400
    ml, mr, mt, mb = 46, 54, 34, 40
    pw, ph = W - ml - mr, H - mt - mb
    x0 = days[0][0]
    span = max((x_end - x0).days, 30)

    def X(d):
        return ml + (d - x0).days / span * pw

    max_bar = max([l for _, l in weeks] + [5])          # left axis: lessons / week
    max_cum = max([c for _, c in cum + proj] + [10])    # right axis: cumulative lessons

    def YL(v):
        return mt + ph - v / max_bar * ph

    def YR(v):
        return mt + ph - v / max_cum * ph

    p = [f'<svg viewBox="0 0 {W} {H}" role="img" '
         f'aria-label="Bars: lessons per week. Line: cumulative lessons, dashed projection to {x_end}.">']

    for i in range(6):
        v = max_bar * i / 5
        y = YL(v)
        p.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{W - mr}" y2="{y:.1f}" stroke="#2a3a40"/>')
        p.append(f'<text x="{ml - 6}" y="{y + 4:.1f}" fill="#8fa6ad" font-size="11" text-anchor="end">{int(round(v))}</text>')
        cv = max_cum * i / 5
        p.append(f'<text x="{W - mr + 6}" y="{y + 4:.1f}" fill="#5d8fb0" font-size="11" text-anchor="start">{int(round(cv)):,}</text>')

    y, m = x0.year, x0.month
    while date(y, m, 1) <= x_end:
        d = date(y, m, 1)
        if d >= x0:
            x = X(d)
            p.append(f'<line x1="{x:.1f}" y1="{mt + ph}" x2="{x:.1f}" y2="{mt + ph + 5}" stroke="#8fa6ad"/>')
            p.append(f'<text x="{x:.1f}" y="{H - 8}" fill="#8fa6ad" font-size="11" text-anchor="middle">{d:%b %y}</text>')
        m += 1
        if m == 13:
            y, m = y + 1, 1

    slot = pw * 7 / span
    bw = slot * 0.72
    for ws, lessons in weeks:
        if lessons <= 0:
            continue
        x = max(X(ws) + (slot - bw) / 2, ml)   # clamp a partial first week at the left edge
        w = min(bw, X(ws + timedelta(days=7)) - x)
        if w <= 0:
            continue
        p.append(f'<rect x="{x:.1f}" y="{YL(lessons):.1f}" width="{w:.1f}" '
                 f'height="{mt + ph - YL(lessons):.1f}" rx="2" fill="#58cc02" fill-opacity="0.85"/>')

    p.append('<polyline points="' + " ".join(f"{X(d):.1f},{YR(c):.1f}" for d, c in cum)
             + '" fill="none" stroke="#1cb0f6" stroke-width="2.5" stroke-linejoin="round"/>')
    p.append('<polyline points="' + " ".join(f"{X(d):.1f},{YR(c):.1f}" for d, c in proj)
             + '" fill="none" stroke="#1cb0f6" stroke-width="2.5" stroke-dasharray="6 5" opacity="0.85"/>')

    x = X(today)
    p.append(f'<line x1="{x:.1f}" y1="{mt}" x2="{x:.1f}" y2="{mt + ph}" stroke="#ff4b4b" stroke-width="1.5"/>')
    p.append(f'<text x="{x:.1f}" y="{mt - 10}" fill="#ff4b4b" font-size="11" text-anchor="middle">today</text>')

    for key, (d, _r, _n, _t) in section_dates.items():
        if d > x_end:
            continue
        x = X(d)
        p.append(f'<line x1="{x:.1f}" y1="{mt}" x2="{x:.1f}" y2="{mt + ph}" stroke="#ffc800" stroke-width="1.5" stroke-dasharray="4 4"/>')
        p.append(f'<text x="{x:.1f}" y="{mt + ph + 17}" fill="#ffc800" font-size="11" text-anchor="middle">S{key} {d:%d %b}</text>')

    p.append('</svg>')
    return "".join(p)


# ---------- page ----------

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; background: #0f171a; color: #e8f1f2;
       font-family: system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
main { max-width: 880px; margin: 0 auto; padding: 16px 14px 40px; }
header { display: flex; justify-content: space-between; align-items: baseline; flex-wrap: wrap; gap: 4px; }
h1 { font-size: clamp(20px, 5vw, 28px); margin: 6px 0; }
.muted { color: #8fa6ad; font-size: 13px; }
.cards { display: grid; gap: 10px; margin: 14px 0; }
.projection { grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); }
.stats { grid-template-columns: repeat(auto-fit, minmax(135px, 1fr)); }
.card { background: #1b262b; border: 1px solid #2a3a40; border-radius: 14px; padding: 14px 16px; }
.card.highlight { border-color: #58cc02; }
.card .label { color: #8fa6ad; font-size: 12px; text-transform: uppercase; letter-spacing: .05em; }
.card .big { font-size: clamp(21px, 6vw, 29px); font-weight: 700; margin-top: 4px; }
.card .sub { color: #8fa6ad; font-size: 13px; margin-top: 4px; }
.chart-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
svg { display: block; width: 100%; min-width: 640px; height: auto; }
.legend { display: flex; gap: 18px; flex-wrap: wrap; color: #8fa6ad; font-size: 13px; margin-top: 10px; }
.sw { display: inline-block; width: 12px; height: 12px; border-radius: 3px; margin-right: 6px; vertical-align: -1px; }
.sw.line { height: 3px; vertical-align: 2px; border-radius: 2px; }
footer { color: #5f7a82; font-size: 12px; margin-top: 18px; line-height: 1.5; }
"""


def render_html(now, today, days, ma7, cum, course, current_unit, sec_dates, svg, proj_end, wanted):
    total = cum[-1][1] if cum else 0
    lessons_today = days[-1][1]
    week_start = today - timedelta(days=today.weekday())
    lessons_week = sum(l for d, l, _ in days if d >= week_start)
    course_title = html.escape(str(course.get("title", "Duolingo")))
    c_fin, c_tot = course.get("finished", 0), course.get("total", 0)
    c_pct = min(100, round(100 * c_fin / c_tot)) if c_tot else 0
    now_line = f" · now: {html.escape(current_unit)}" if current_unit else ""

    def sec_card(n):
        label = f"Section {n}"
        if n not in sec_dates:
            if ma7 <= 0:
                return (f'<div class="card"><div class="label">{label} done</div>'
                        f'<div class="big">—</div><div class="sub">no lessons in the last 7 days</div></div>')
            return (f'<div class="card"><div class="label">{label} done</div>'
                    f'<div class="big">—</div><div class="sub">section not found in current course</div></div>')
        d, r, days_needed, through = sec_dates[n]
        if through == 0:
            return (f'<div class="card highlight"><div class="label">{label} done</div>'
                    f'<div class="big">done 🎉</div><div class="sub">no lessons remaining</div></div>')
        return (f'<div class="card highlight"><div class="label">{label} done</div>'
                f'<div class="big">{d:%d %b %Y}</div>'
                f'<div class="sub">in {days_needed} days · {through:,} lessons to go '
                f'({r:,} in S{n}) @ {ma7:.1f}/day</div></div>')

    proj_cards = "".join(sec_card(n) for n in wanted)
    stats = f"""
  <section class="cards stats">
    <div class="card"><div class="label">Lessons today</div><div class="big">{lessons_today}</div></div>
    <div class="card"><div class="label">This week</div><div class="big">{lessons_week}</div></div>
    <div class="card"><div class="label">7-day avg</div><div class="big">{ma7:.1f}<span class="muted">/day</span></div></div>
    <div class="card"><div class="label">Course progress</div><div class="big">{c_pct}%</div>
      <div class="sub">{c_fin:,} / {c_tot:,} lessons</div></div>
    <div class="card"><div class="label">Total lessons</div><div class="big">{total:,}</div></div>
  </section>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Duolingo dashboard</title>
<style>{CSS}</style>
</head>
<body>
<main>
  <header>
    <h1>🦉 {course_title}</h1>
    <span class="muted">updated {now:%Y-%m-%d %H:%M %Z}{now_line}</span>
  </header>

  <section class="cards projection">
    {proj_cards}
  </section>

  {stats}

  <section class="card">
    <div class="chart-scroll">{svg}</div>
    <div class="legend">
      <span><span class="sw" style="background:#58cc02"></span>lessons / week (left axis)</span>
      <span><span class="sw line" style="background:#1cb0f6"></span>cumulative lessons (right axis)</span>
      <span><span class="sw line" style="background:#1cb0f6;opacity:.7"></span>projection @ {ma7:.1f}/day to {proj_end:%d %b %Y}</span>
      <span><span class="sw line" style="background:#ffc800"></span>section milestones</span>
    </div>
  </section>

  <footer>
    Generated {now:%Y-%m-%d %H:%M %Z} by duo_dashboard.py from Duolingo's unofficial endpoints
    (xp_summaries for history; users/&#123;id&#125;?fields=currentCourse for live path progress).
    Projection assumes a constant {ma7:.1f} lessons/day (7-day moving average).
  </footer>
</main>
</body>
</html>
"""


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="output HTML file (overrides DASHBOARD_OUT)")
    ap.add_argument("--days", type=int, help="history window in days (overrides DUO_HISTORY_DAYS)")
    ap.add_argument("--chart-start", help="ISO date the graph's x-axis starts at (overrides DUO_CHART_START)")
    ap.add_argument("--mock", help="saved xp_summaries response (offline)")
    ap.add_argument("--mock-course", help="saved currentCourse response (offline)")
    ap.add_argument("--debug", action="store_true", help="print the raw API responses")
    args = ap.parse_args()

    cfg = load_config(args)
    now = datetime.now(cfg["tz"])
    today = now.date()
    if cfg["proj_end"] <= today:
        sys.exit(f"DUO_PROJECTION_END ({cfg['proj_end']}) must be in the future")
    start = today - timedelta(days=cfg["days"] - 1)

    data = fetch_range(cfg, start, today)
    course_raw = fetch_current_course(cfg)
    if args.debug:
        print(json.dumps(data, indent=2)[:4000])
        print(json.dumps(course_raw, indent=2)[:4000])

    days = daily_series(data, start, today)
    weeks = weekly(days)
    cum = cumulative(days)
    ma7 = moving_avg7(days)
    total = cum[-1][1] if cum else 0
    proj = [(today + timedelta(days=n), total + ma7 * n)
            for n in range((cfg["proj_end"] - today).days + 1)]

    course = parse_course(course_raw)
    current_unit = course["current_unit"]
    sec_dates = project_sections(course["sections"], today, ma7, cfg["proj_sections"])

    # the graph is windowed to start at DUO_CHART_START; stats/MA above use the full history
    x_start = max(days[0][0], min(cfg["chart_start"], today))
    chart_days = [d for d in days if d[0] >= x_start] or days
    chart_cum = [(d, c) for d, c in cum if d >= chart_days[0][0]] or cum
    chart_weeks = [(ws, l) for ws, l in weeks if ws + timedelta(days=7) > chart_days[0][0]]

    svg = render_chart(chart_days, chart_weeks, chart_cum, proj, today, cfg["proj_end"], sec_dates)
    page = render_html(now, today, days, ma7, cum, course, current_unit,
                       sec_dates, svg, cfg["proj_end"], cfg["proj_sections"])
    cfg["out"].write_text(page, encoding="utf-8")

    print(f"Wrote {cfg['out']} ({cfg['out'].stat().st_size:,} bytes)")
    print(f"{today}: {days[-1][1]} lessons today, 7-day avg {ma7:.2f}/day, {total:,} total")
    print(f"course: {course['finished']:,}/{course['total']:,} path lessons done"
          + (f", now at {current_unit}" if current_unit else ""))
    for n in sorted(sec_dates):
        d, r, days_needed, through = sec_dates[n]
        print(f"section {n}: projected {d} ({through:,} lessons to go, {days_needed} days at {ma7:.1f}/day)")


if __name__ == "__main__":
    main()
