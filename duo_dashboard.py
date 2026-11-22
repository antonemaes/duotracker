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
  3. Builds weekly lesson totals (bars) and a cumulative-lessons line. The graph ends at
     "today" - no projection line; the section dates are in the cards above the stats.
  4. Projects each section's completion date from its live remaining lessons divided by the
     7-day average. The cards go below the hero ring.

Config (environment variables)
  DUO_JWT                 required (unless mocked)  Duolingo jwt_token cookie
  DUO_TZ                  optional  IANA time zone                     (default Europe/Zurich)
  DUO_MOCK_JSON           optional  saved xp_summaries response, for offline testing
  DUO_MOCK_COURSE_JSON    optional  saved currentCourse response, for offline testing
  DUO_HISTORY_DAYS        optional  days of history to fetch           (default 365)
  DUO_CHART_START         optional  ISO date the graph's x-axis starts (default 2026-10-01)
  DUO_DAILY_TARGET        optional  daily lessons target for the hero ring (default 25)
  DUO_WEEKLY_TARGET       optional  weekly lessons target; bars >= this are green (default 175 = 25/day)
  DUO_PROJECTION_SECTIONS optional  comma-separated section numbers  (default 5,6)
  DASHBOARD_OUT           optional  output HTML file                   (default index.html)
  DUO_REPO                optional  GitHub "owner/repo" for the page's refresh link
                                                      (default antonemaes/duotracker)

Both endpoints are unofficial and undocumented; either could change or break at any time.

Flags
  --out PATH         output file (overrides DASHBOARD_OUT)
  --days N           history window in days (overrides DUO_HISTORY_DAYS)
  --chart-start DATE ISO date the graph starts at (overrides DUO_CHART_START)
  --daily-target N   daily lessons target for the hero ring (overrides DUO_DAILY_TARGET)
  --weekly-target N  weekly lessons target for bar coloring (overrides DUO_WEEKLY_TARGET)
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
        "daily_target": args.daily_target or int(env("DUO_DAILY_TARGET", "25")),
        "weekly_target": args.weekly_target or int(env("DUO_WEEKLY_TARGET", "175")),
        "proj_sections": sorted(int(s) for s in env("DUO_PROJECTION_SECTIONS", "5,6").split(",")),
        "out": Path(args.out or env("DASHBOARD_OUT", "index.html")),
        "repo": env("DUO_REPO", "antonemaes/duotracker"),
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

def render_chart(days, weeks, cum, today, week_target):
    W, H = 720, 400
    ml, mr, mt, mb = 46, 54, 34, 40
    pw, ph = W - ml - mr, H - mt - mb
    x0 = days[0][0]
    x_end = today                       # the graph ends at today - no projection
    span = max((x_end - x0).days, 30)

    def X(d):
        return ml + (d - x0).days / span * pw

    max_bar = max([l for _, l in weeks] + [week_target, 5])   # left axis: lessons / week
    max_cum = max([c for _, c in cum] + [10])                 # right axis: cumulative lessons

    def YL(v):
        return mt + ph - v / max_bar * ph

    def YR(v):
        return mt + ph - v / max_cum * ph

    p = [f'<svg viewBox="0 0 {W} {H}" role="img" '
         f'aria-label="Bars: lessons per week, colored by the {week_target}/week target. Line: cumulative lessons up to {x_end}.">']

    for i in range(6):
        v = max_bar * i / 5
        y = YL(v)
        p.append(f'<line x1="{ml}" y1="{y:.1f}" x2="{W - mr}" y2="{y:.1f}" stroke="#2a3a40"/>')
        p.append(f'<text x="{ml - 6}" y="{y + 4:.1f}" fill="#8fa6ad" font-size="11" text-anchor="end">{int(round(v))}</text>')
        cv = max_cum * i / 5
        p.append(f'<text x="{W - mr + 6}" y="{y + 4:.1f}" fill="#5d8fb0" font-size="11" text-anchor="start">{int(round(cv)):,}</text>')

    # weekly-target line
    yt = YL(week_target)
    p.append(f'<line x1="{ml}" y1="{yt:.1f}" x2="{W - mr}" y2="{yt:.1f}" stroke="#e8f1f2" stroke-width="1" stroke-dasharray="3 4" opacity="0.7"/>')
    p.append(f'<text x="{ml + 4}" y="{yt - 4:.1f}" fill="#e8f1f2" font-size="10" opacity="0.8">target {week_target}/wk</text>')

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
        x = max(X(ws) + (slot - bw) / 2, ml)   # clamp a partial first week at the left edge
        w = min(bw, X(ws + timedelta(days=7)) - x)
        if w <= 0:
            continue
        color = "#58cc02" if lessons >= week_target else "#ff4b4b"
        h = mt + ph - YL(lessons)
        if lessons <= 0:
            h = 3   # baseline tick marks a week with zero lessons
        p.append(f'<rect x="{x:.1f}" y="{mt + ph - h:.1f}" width="{w:.1f}" height="{h:.1f}" '
                 f'rx="2" fill="{color}" fill-opacity="0.85"/>')

    p.append('<polyline points="' + " ".join(f"{X(d):.1f},{YR(c):.1f}" for d, c in cum)
             + '" fill="none" stroke="#1cb0f6" stroke-width="2.5" stroke-linejoin="round"/>')

    x = X(today)
    p.append(f'<line x1="{x:.1f}" y1="{mt}" x2="{x:.1f}" y2="{mt + ph}" stroke="#ff4b4b" stroke-width="1.5"/>')
    p.append(f'<text x="{x:.1f}" y="{mt - 10}" fill="#ff4b4b" font-size="11" text-anchor="end">today</text>')

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
header > span { display: inline-flex; align-items: center; gap: 10px; flex-wrap: wrap; }
h1 { font-size: clamp(20px, 5vw, 28px); margin: 6px 0; }
a.refresh { color: #58cc02; border: 1px solid #58cc02; border-radius: 999px; padding: 3px 12px;
            font-size: 13px; text-decoration: none; white-space: nowrap; }
a.refresh:hover { background: #58cc02; color: #0f171a; }
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
#refreshbar { position: sticky; top: 0; z-index: 10; display: flex; align-items: center; gap: 10px;
              background: #1b262b; border-bottom: 1px solid #2a3a40; margin: -16px -14px 12px;
              padding: 8px 14px; font-size: 13px; color: #8fa6ad; }
#refreshbar.hidden { display: none; }
#refreshbar .track { flex: 1; height: 6px; background: #2a3a40; border-radius: 3px; overflow: hidden; }
#refreshbar .bar { height: 100%; width: 40%; background: #58cc02; border-radius: 3px; animation: slide 1.2s ease-in-out infinite; }
@keyframes slide { 0% { margin-left: -40%; } 100% { margin-left: 100%; } }
.hero { display: flex; align-items: center; gap: 18px; flex-wrap: wrap; margin: 14px 0; }
.donut { width: 150px; height: 150px; flex: 0 0 auto; }
.donut-num { fill: #e8f1f2; font-size: 42px; font-weight: 700; }
.donut-sub { fill: #8fa6ad; font-size: 13px; }
.hero .big { font-size: clamp(26px, 7vw, 36px); }
"""


def donut(lessons, target, size=170, stroke=14):
    """Circular progress ring: lessons completed today vs the daily target."""
    r = (size - stroke) / 2
    c = 2 * math.pi * r
    pct = min(1.0, lessons / target) if target > 0 else 0.0
    color = "#58cc02" if lessons >= target else "#1cb0f6"
    off = c * (1 - pct)
    cx = size / 2
    return (f'<svg viewBox="0 0 {size} {size}" class="donut" role="img" '
            f'aria-label="{lessons} of {target} lessons today ({round(pct * 100)}%)">'
            f'<circle cx="{cx}" cy="{cx}" r="{r:.1f}" fill="none" stroke="#2a3a40" stroke-width="{stroke}"/>'
            f'<circle cx="{cx}" cy="{cx}" r="{r:.1f}" fill="none" stroke="{color}" stroke-width="{stroke}" '
            f'stroke-linecap="round" stroke-dasharray="{c:.1f}" stroke-dashoffset="{off:.1f}" '
            f'transform="rotate(-90 {cx} {cx})"/>'
            f'<text x="50%" y="48%" text-anchor="middle" class="donut-num">{lessons}</text>'
            f'<text x="50%" y="62%" text-anchor="middle" class="donut-sub">/ {target} today · {round(pct * 100)}%</text>'
            f'</svg>')


# Inline script for the refresh progress bar. Kept out of the f-string below so its
# braces don't need escaping. Raw string so the regex \d stays literal.
REFRESH_JS = r"""<script>
(function () {
  var KEY = "duoDashRefreshAt";
  var bar = document.getElementById("refreshbar");
  var msg = document.getElementById("refreshmsg");
  var updatedEl = document.querySelector("header .muted");
  var m0 = updatedEl && updatedEl.textContent.match(/updated (\d{4}-\d{2}-\d{2} \d{2}:\d{2})/);
  var cur = m0 ? m0[1] : "";
  var pending = null;
  try { pending = localStorage.getItem(KEY); } catch (e) {}
  var btn = document.querySelector("a.refresh");
  if (btn) btn.addEventListener("click", function () {
    try { localStorage.setItem(KEY, new Date().toLocaleTimeString()); } catch (e) {}
  });
  if (!pending) return;
  bar.classList.remove("hidden");
  msg.textContent = "Refresh requested at " + pending + " - this page reloads itself when the new dashboard is live.";
  var t0 = Date.now();
  var iv = setInterval(function () {
    if (Date.now() - t0 > 10 * 60 * 1000) {
      clearInterval(iv);
      try { localStorage.removeItem(KEY); } catch (e) {}
      msg.textContent = "Still waiting - check the Actions tab; you can also just reload this page.";
      return;
    }
    fetch(location.pathname, { cache: "no-store" }).then(function (r) { return r.text(); }).then(function (t) {
      var m = t.match(/updated (\d{4}-\d{2}-\d{2} \d{2}:\d{2})/);
      if (m && cur && m[1] !== cur) {
        clearInterval(iv);
        try { localStorage.removeItem(KEY); } catch (e) {}
        location.href = location.pathname;
      }
    }).catch(function () {});
  }, 15000);
})();
</script>"""


def render_html(now, today, days, ma7, cum, course, current_unit, sec_dates, svg, wanted, week_target, repo, daily_target):
    total = cum[-1][1] if cum else 0
    lessons_today = days[-1][1]
    week_start = today - timedelta(days=today.weekday())
    lessons_week = sum(l for d, l, _ in days if d >= week_start)
    hit = lessons_today >= daily_target
    hero = f"""
  <section class="card hero">
    {donut(lessons_today, daily_target)}
    <div>
      <div class="label">Lessons today</div>
      <div class="big">{lessons_today}<span class="muted"> / {daily_target}</span></div>
      <div class="sub">{round(min(1, lessons_today / daily_target) * 100) if daily_target else 0}% of today's target{' — hit 🎉' if hit else ''}</div>
    </div>
  </section>"""
    course_title = html.escape(str(course.get("title", "Duolingo")))
    c_fin, c_tot = course.get("finished", 0), course.get("total", 0)
    c_pct = min(100, round(100 * c_fin / c_tot)) if c_tot else 0
    now_line = f" · now: {html.escape(current_unit)}" if current_unit else ""
    refresh_url = f"https://github.com/{html.escape(repo)}/actions/workflows/dashboard.yml"

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
    <span>
      <span class="muted">updated {now:%Y-%m-%d %H:%M %Z}{now_line}</span>
      <a class="refresh" href="{refresh_url}" target="_blank" rel="noopener" title="Opens the workflow page - press 'Run workflow' there; this page then shows a progress bar and reloads automatically">🔄 refresh</a>
    </span>
  </header>

  <div id="refreshbar" class="hidden"><span id="refreshmsg"></span><div class="track"><div class="bar"></div></div></div>

  {hero}

  <section class="cards projection">
    {proj_cards}
  </section>

  {stats}

  <section class="card">
    <div class="chart-scroll">{svg}</div>
    <div class="legend">
      <span><span class="sw" style="background:#58cc02"></span>week ≥ {week_target} lessons</span>
      <span><span class="sw" style="background:#ff4b4b"></span>week &lt; {week_target} (left axis)</span>
      <span><span class="sw line" style="background:#1cb0f6"></span>cumulative lessons (right axis)</span>
    </div>
  </section>

  <footer>
    Generated {now:%Y-%m-%d %H:%M %Z} by duo_dashboard.py from Duolingo's unofficial endpoints
    (xp_summaries for history; users/&#123;id&#125;?fields=currentCourse for live path progress).
    Section dates assume a constant {ma7:.1f} lessons/day (7-day moving average).
  </footer>
</main>
{REFRESH_JS}
</body>
</html>
"""


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="output HTML file (overrides DASHBOARD_OUT)")
    ap.add_argument("--days", type=int, help="history window in days (overrides DUO_HISTORY_DAYS)")
    ap.add_argument("--chart-start", help="ISO date the graph's x-axis starts at (overrides DUO_CHART_START)")
    ap.add_argument("--daily-target", type=int, help="daily lessons target for the hero ring (overrides DUO_DAILY_TARGET)")
    ap.add_argument("--weekly-target", type=int, help="weekly lessons target for bar coloring (overrides DUO_WEEKLY_TARGET)")
    ap.add_argument("--mock", help="saved xp_summaries response (offline)")
    ap.add_argument("--mock-course", help="saved currentCourse response (offline)")
    ap.add_argument("--debug", action="store_true", help="print the raw API responses")
    args = ap.parse_args()

    cfg = load_config(args)
    now = datetime.now(cfg["tz"])
    today = now.date()
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

    course = parse_course(course_raw)
    current_unit = course["current_unit"]
    sec_dates = project_sections(course["sections"], today, ma7, cfg["proj_sections"])

    # the graph is windowed to start at DUO_CHART_START; stats/MA above use the full history
    x_start = max(days[0][0], min(cfg["chart_start"], today))
    chart_days = [d for d in days if d[0] >= x_start] or days
    chart_cum = [(d, c) for d, c in cum if d >= chart_days[0][0]] or cum
    chart_weeks = [(ws, l) for ws, l in weeks if ws + timedelta(days=7) > chart_days[0][0]]

    svg = render_chart(chart_days, chart_weeks, chart_cum, today, cfg["weekly_target"])
    page = render_html(now, today, days, ma7, cum, course, current_unit,
                       sec_dates, svg, cfg["proj_sections"], cfg["weekly_target"], cfg["repo"],
                       cfg["daily_target"])
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
