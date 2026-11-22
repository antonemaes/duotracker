# duo-nudge 🦉

📊 **[Dashboard](https://antonemaes.github.io/duotracker/)** — live lesson stats and section-completion projections (GitHub Pages, updated daily).

Sends a push notification to your iPhone saying how many minutes of Duolingo lessons you have left today. It stays quiet once you've done 60 minutes.

```
🦉 37 min of Duolingo left
Done today: 23 / 60 min (6 lessons)
4.6 h until midnight
```

**Stack:** Python (standard library + `tzdata`, see `requirements.txt`), Duolingo's *unofficial* `xp_summaries` endpoint, GitHub Actions for the schedule, and [ntfy](https://ntfy.sh) for push notifications.

## Setup (about 15 min)

1. **Install ntfy on your iPhone** from the App Store. Subscribe to a long random topic name, e.g. `duo-anton-8f3k29xq`. Anyone who knows the name can read the topic, so don't make it guessable.
2. **Get your Duolingo token.** Log in at duolingo.com on a computer, open DevTools → Application → Cookies → `https://www.duolingo.com`, and copy the value of `jwt_token`.
   ⚠️ This token gives full access to your account. Never commit it. If it leaks, change your Duolingo password, which invalidates it.
3. **Set up a virtual environment and your secrets:**
   ```bash
   py -3.12 -m venv .venv                 # Windows (or: python3 -m venv .venv)
   .venv\Scripts\pip install -r requirements.txt
   copy .env.example .env                  # then edit .env and fill in DUO_JWT and NTFY_TOPIC
   ```
   `.env` is gitignored — nothing loads it automatically anymore. Locally, export it into your shell first: `set -a; source .env; set +a` (Git Bash). In GitHub Actions, secrets are injected as real environment variables, which the scripts read directly.
4. **Test locally:**
   ```bash
   .venv\Scripts\python duo_nudge.py --force --debug --dry-run   # check the raw fields
   .venv\Scripts\python duo_nudge.py --force                     # send a real push
   ```
5. **Push to a GitHub repo.** In Settings → Secrets and variables → Actions, add the secrets `DUO_JWT` and `NTFY_TOPIC`. Keep it *private* for the nudge alone — but note the dashboard on Pages needs a *public* repo on a free plan (see below).
6. **Test the workflow:** go to Actions → *Duolingo nudge* → *Run workflow*.

## Tuning

Edit the `env:` block in `.github/workflows/nudge.yml`:

| Variable | Default | Meaning |
|---|---|---|
| `DUO_GOAL_MINUTES` | `60` | Daily goal |
| `DUO_NUDGE_HOURS` | `12,16,19,21,22` | Local hours to nudge. The last one is marked "last call" |
| `DUO_TZ` | `Europe/Zurich` | Your time zone |

## Dashboard 📊

`duo_dashboard.py` generates **`index.html`** — a single self-contained, mobile-friendly page (no external scripts/fonts, works offline and on GitHub Pages). It makes two unofficial API calls:

- `xp_summaries` for the lesson history (small), and
- `users/{id}?fields=currentCourse` (~9 MB) — `currentCourse.pathSectioned` has every section, unit and step with `totalSessions`/`finishedSessions`, so **remaining lessons per section are read live**, no hand-maintained config. That's why this runs in the *daily* dashboard job only, never in the hourly nudge job.

The page shows:

- **Hero (very top):** a circular ring with **lessons completed today** vs the daily target (25, `DUO_DAILY_TARGET`) — blue while in progress, green once hit.
- **Date cards:** projected completion dates for **Section 5** and **Section 6** — live remaining lessons (counting all unfinished sections up to that one) divided by your **7-day moving average** of lessons/day.
- **Stat cards:** lessons today, this week, 7-day average, course progress (path lessons done/total, live), total lessons.
- **Chart:** bars = lessons per week (left axis), **green if the week hit the target (≥175 = 25/day × 7), red if below** (a small red tick marks a zero-lesson week), with a dashed target line. Blue line = cumulative lessons (right axis). The graph ends at today — no projection line; the section dates are in the cards. The header also shows the unit you're currently on.

**Daily updates:** `.github/workflows/dashboard.yml` runs once a day, regenerates `index.html`, and commits it back to the repo. **Manual refresh:** the 🔄 refresh button in the page header links to the workflow page (Actions → *Run workflow*), or use the *Run workflow* button directly in the Actions tab. After clicking 🔄, the dashboard shows an animated progress bar at the top and reloads itself automatically once the new `index.html` is deployed (polls every 15 s, gives up after 10 min). Two one-time setup steps:

1. Settings → Actions → General → Workflow permissions → allow **Read and write permissions** (so the workflow can commit).
2. Settings → Pages → Source: *Deploy from a branch* → `main` / `(root)`. Because the file is named `index.html`, the dashboard is served at the site **root**: `https://antonemaes.github.io/duotracker/` — add that to your phone's home screen. (Note: on a free GitHub plan, Pages only serves *public* repos — make the repo public, or use a paid plan to keep it private.)

**Local test** (offline, uses the checked-in mock data — a saved `xp_summaries` range and a saved `currentCourse` tree):

```bash
.venv\Scripts\python duo_dashboard.py --mock tests/mock_range.json --mock-course tests/mock_course.json --days 150
# writes ./index.html — open it in a browser
```

Dashboard env vars (set in the `env:` block of `.github/workflows/dashboard.yml`):

| Variable | Default | Meaning |
|---|---|---|
| `DUO_HISTORY_DAYS` | `365` | How much history to fetch (stats/averages use all of it) |
| `DUO_CHART_START` | `2026-10-01` | Where the graph's x-axis starts (Oct 2026) |
| `DUO_DAILY_TARGET` | `25` | Hero ring: lessons-today target (turns green when hit) |
| `DUO_WEEKLY_TARGET` | `175` | Weekly bar target for green/red coloring (25 lessons/day × 7) |
| `DUO_PROJECTION_SECTIONS` | `5,6` | Which sections get completion-date cards |
| `DUO_MOCK_JSON` / `DUO_MOCK_COURSE_JSON` | — | Saved API responses for offline runs |
| `DASHBOARD_OUT` | `index.html` | Output file (name it `index.html` so Pages serves it at the site root) |
| `DUO_REPO` | `antonemaes/duotracker` | `owner/repo` used for the in-page 🔄 refresh link |

## Things to know

- **Unofficial API.** Duolingo can change or block it at any time. If the script says `totalSessionTime` is missing, run with `--debug` and update the field name.
- **Session time comes from Duolingo,** so it counts lesson time, not time spent idle in the app.
- **GitHub cron is best-effort.** Scheduled runs can be late by 5–30 minutes. The script checks the local hour itself, so a late run still works.
- **Usage:** about 14 short runs a day, roughly 420 Actions minutes a month, well inside the free tier for private repos.

## Ideas to extend it (good learning steps)

- Write unit tests with `pytest` for `build_message` and `minutes_today`.
- Send one "🎉 goal met" message per day. This needs state, e.g. a GitHub Actions cache or a small JSON file committed back to the repo.
- Add a weekly summary on Sunday evenings using a 7-day `xp_summaries` range.
- Build an iOS Shortcut that calls the same endpoint with *Get Contents of URL*, so you can check on demand from a home-screen widget.
