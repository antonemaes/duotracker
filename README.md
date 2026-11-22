# duo-nudge 🦉

📊 **[Dashboard](https://antonemaes.github.io/duotracker/)**: live lesson stats, streak and section finish dates (GitHub Pages, refreshed about every 5 minutes).

Sends a push notification to your iPhone saying how many minutes of Duolingo lessons you have left today. It stays quiet once you've done 60 minutes.

```
🦉 37 min to go · 23 of 60 done
6 lessons so far · 🔥 47-day streak
About 10 more lessons · 4 h 36 min left today.
```

**Stack:** Python (standard library + `tzdata`, see `requirements.txt`), Duolingo's *unofficial* `xp_summaries` and `users` endpoints, GitHub Actions for the schedule, GitHub Pages for the dashboard, and [ntfy](https://ntfy.sh) for push notifications.

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
   `.env` is gitignored, and nothing loads it automatically. Locally, export it into your shell first: `set -a; source .env; set +a` (Git Bash). In GitHub Actions, secrets are injected as real environment variables, which the scripts read directly.
4. **Test locally:**
   ```bash
   .venv\Scripts\python duo_nudge.py --force --debug --dry-run   # check the raw fields
   .venv\Scripts\python duo_nudge.py --force                     # send a real push
   ```
5. **Push to a GitHub repo.** In Settings → Secrets and variables → Actions, add the secrets `DUO_JWT` and `NTFY_TOPIC`. The dashboard on Pages needs a *public* repo on a free plan (see below).
6. **Test the workflow:** go to Actions → *Duolingo nudge* → *Run workflow*.

## Notifications 🔔

`duo_nudge.py` picks one of four messages, most urgent first. Each title leads with the number that matters, and the body says what's done and what to do next:

| State | When | Title (example) | Priority |
|---|---|---|---|
| Last call | the last nudge hour | `🚨 Last call: 37 min to go` (body asks you to save the streak if today hasn't extended it) | high |
| Behind pace | more than 10 min behind a steady pace (see below) | `⚠️ Behind pace: 40 min to go` | high |
| Not started | no lessons yet, and not behind (in practice the first nudge) | `🦉 Time for today's 60 min` | default |
| On pace | otherwise | `🦉 37 min to go · 23 of 60 done` | default |

**Steady pace** runs from the first nudge hour to the last: 0 min at 12:00, the full goal at 22:00. With a 60-minute goal that's about 25 min at the 16:07 nudge, 43 min at 19:07 and 55 min at 21:07. You're behind pace when you're more than 10 minutes short of that (`PACE_SLACK_MIN` in `duo_nudge.py`), so a lesson or two behind doesn't trigger a high-priority alert.

- **Tap** opens Duolingo (`duolingo://`). Two buttons: **Open Duolingo** and **Dashboard** (`DUO_DASHBOARD_URL`).
- **No emoji tags.** ntfy turns tags like `owl` into an emoji and adds it to the title, which used to give you two owls. The emoji now lives only in the title.
- **One notification per day.** Each day's nudges share an `X-Sequence-ID`, so where the ntfy client supports it, a new nudge replaces the earlier one instead of stacking.
- **Plain text** on purpose: the iOS app doesn't render Markdown.
- The streak comes from a tiny extra call (`users/{id}?fields=streak`). If that call fails, the nudge just leaves the streak out.

## Tuning

Edit the `env:` block in `.github/workflows/nudge.yml`:

| Variable | Default | Meaning |
|---|---|---|
| `DUO_GOAL_MINUTES` | `60` | Daily goal |
| `DUO_NUDGE_HOURS` | `12,16,19,21,22` | Local hours to nudge. The first and last also set the steady-pace window, and the last one is the "last call" |
| `DUO_TZ` | `Europe/Zurich` | Your time zone |
| `DUO_DASHBOARD_URL` | the Pages URL | Where the notification's *Dashboard* button goes |

## Dashboard 📊

**How it stays cheap at a 5-minute cadence.** `duo_dashboard.py` makes two small calls:

- `xp_summaries` for the lesson history (~1 KB gzipped), and
- `users/{id}` with a **nested field filter**, `currentCourse{pathSectioned{units{levels{finishedSessions,totalSessions,state,type}}}}` plus streak and XP. The unfiltered `currentCourse` is **~9.3 MB**. Filtered and gzipped, it is **~22 KB** and comes back in about 0.6 s.

It writes a folder (`_site/`) with:

- **`data.json`** (~4 KB): everything the page shows.
- **`index.html`**: the static design in `web/index.html`, with the same data inlined so the first paint needs no extra request.
- The icons, web-app manifest and social preview image from `web/`.

The open page re-fetches `data.json` every minute, and again whenever you come back to the tab. New numbers count up in place, with no reload. The pill in the header shows how fresh the data is (green = under 15 min old). Tap it (**↻**) to start an update right away (see [One-tap refresh button](#one-tap-refresh-button)).

**What's on the page** (mobile-first; 2 columns on tablets, a 12-column grid on desktop; light and dark mode follow the system):

- **Today:** activity-style rings for lessons vs `DUO_DAILY_TARGET` (outer) and minutes vs `DUO_GOAL_MINUTES` (inner), streak, XP, and how many lessons are still to go.
- **Level finish cards** for `DUO_PROJECTION_GROUPS`: by default **B1 (Sections 5–6)** and **B2 (Sections 7–8)**. Each card shows the projected date the level is finished, days left, lessons to go (counting every unfinished lesson up to the level's last section), a progress bar across the level's sections, and a what-if date at your daily target pace.
- **Key numbers:** this week vs target, the daily average with its change vs the 7 days before, course % complete, and total XP.
- **Activity:** Days / Weeks / Total views, opening on **Days**: lessons per day for the last 4 weeks, with a dashed line at the daily target. Weeks show volume only (no target line), and Total is the cumulative line. Tap or hover any bar for details, and *Show as table* gives the same numbers as a table.
- **Consistency:** a calendar heatmap since `DUO_CHART_START`, plus active days, target days, best day and lessons per active day.
- **Course path:** every section with its CEFR level, progress and its own ETA. "You're here" marks your current unit.
- **Add to Home Screen** on iPhone gives a full-screen app with its own icon. Shared links show a preview card (`og.png`).

**How the numbers are counted:**

- Sections are numbered **like the app**: Section 1 is the API's index 0. (The old page was off by one, so its "Section 5" card was really Section 6.)
- A level counts as done once Duolingo marks it passed, even if you jumped past it. **Unit reviews are left out:** Duolingo marks them passed when you finish a unit, whether or not you did them.
- The finish dates use the **average of the last 7 full days**. Today isn't included, so the dates stay stable through the day instead of jumping every 5 minutes.

### Deploying (one-time setup)

`.github/workflows/dashboard.yml` builds `_site/` and **deploys it straight to Pages**. Nothing is committed back, so the git history stays clean. It runs on GitHub's schedule (every 5 minutes, best-effort), when you tap the dashboard's **↻** button, and on pushes that touch `web/` or the generator.

1. **Settings → Pages → Build and deployment → Source: _GitHub Actions_.** This replaces the old "Deploy from a branch" setup.
2. Free plan: Pages only serves *public* repos.
3. **Settings → Actions → General → Workflow permissions: _Read repository contents_.** No workflow commits anymore, and each workflow asks for exactly the permissions it needs (`contents: read`, plus `pages`/`id-token` for the deploy).

Then *Run workflow* once, or push. The site stays at `https://antonemaes.github.io/duotracker/`.

Good to know:

- **GitHub's own schedule is best-effort.** A `*/5` schedule ran only once in its first hour here, and the old daily run was often 30–60 minutes late. When you need fresh numbers now (say, before showing the dashboard), tap **↻**.
- **Actions minutes:** each run takes about 30 to 40 s. That's free on public repos (a private repo would use roughly 4,000+ minutes a month).
- **60-day rule:** GitHub pauses *scheduled* workflows in a repo with no commits for 60 days. The ↻ button still works then, but the automatic updates and the nudges stop. GitHub emails you before it pauses anything, and any push or a click on *Enable workflow* turns it back on.

### One-tap refresh button

The pill at the top right of the dashboard ("Live · 3 min ago ↻") starts the dashboard workflow on GitHub. It then shows *Updating… 0:35* and swaps in the fresh numbers when they land, usually within a minute.

Starting a workflow needs a GitHub token, and a public page can't contain one. So the button asks for the token **once per device** and keeps it in that browser's storage only. It's never in the page or the repo, and visitors without it just see an explanation and a link to GitHub. You create the token yourself, so it never passes through anyone else:

1. **Create a token that can only start workflows in this repo.** GitHub → your avatar → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** → *Generate new token*.
   - Name: `duotracker refresh`. Expiration: up to a year (put a reminder in your calendar to renew it).
   - Repository access: **Only select repositories** → `duotracker`.
   - Permissions → Repository permissions → **Actions: Read and write**. Leave everything else at *No access* (*Metadata: Read-only* is added automatically).
   - Generate, and copy the token (it's shown once).

   This token can start, cancel and re-run workflows in this repo. It can't read your secrets, change code or touch other repos.
2. **Paste it into the dashboard.** Open the dashboard on your phone, tap **↻**, paste the token, and tap *Save and refresh*. Do this once on each device and browser you use. A home-screen web app on iPhone keeps its own storage, so it asks once more.
3. **Remove it** any time with *Forget the refresh token on this device* in the page footer, or revoke it on GitHub.

If the token is wrong or expired, the button says so and removes it, and you can paste a new one.

**Optional, always fresh without tapping:** the same token can drive a free outside timer. At [cron-job.org](https://cron-job.org), create a job that runs every 5 minutes and sends a **POST** to `https://api.github.com/repos/antonemaes/duotracker/actions/workflows/dashboard.yml/dispatches` with the headers `Authorization: Bearer <token>`, `Accept: application/vnd.github+json` and `X-GitHub-Api-Version: 2022-11-28`, and the body `{"ref":"main"}`. A test run should answer **204**.

**Local preview** (offline, uses the checked-in synthetic mock data):

```bash
.venv\Scripts\python duo_dashboard.py --mock tests/mock_range.json --mock-course tests/mock_course.json --days 150
.venv\Scripts\python -m http.server 8765 --directory _site   # then open http://localhost:8765
```

(Opening `_site/index.html` directly also works, because the data is inlined. Only the live polling needs the server.)

Dashboard env vars (set in the `env:` block of `.github/workflows/dashboard.yml`):

| Variable | Default | Meaning |
|---|---|---|
| `DUO_HISTORY_DAYS` | `365` | How much history to fetch |
| `DUO_CHART_START` | `2026-10-01` | Where the weekly/total charts and the heatmap start |
| `DUO_DAILY_TARGET` | `25` | Lessons per day: outer ring, the target line on the daily chart, heatmap, what-if dates |
| `DUO_WEEKLY_TARGET` | `175` | Lessons per week: the *This week* tile only (25/day × 7) |
| `DUO_GOAL_MINUTES` | `60` | Minutes per day: inner ring (same variable as the nudge) |
| `DUO_PROJECTION_GROUPS` | `B1=5,6;B2=7,8` | Finish-date cards: `name=sections`, separated by `;` (sections numbered like the app). A plain list such as `5,6` gives one card per section |
| `DUO_REPO` | `antonemaes/duotracker` | `owner/repo` for the page's links (the workflow passes `github.repository`) |
| `DUO_PAGE_URL` | `https://<owner>.github.io/<repo>/` | Public URL, used for the link-preview tags |
| `DASHBOARD_OUT_DIR` | `_site` | Output folder |
| `DUO_MOCK_JSON` / `DUO_MOCK_COURSE_JSON` | none | Saved API responses for offline runs |

## Tests

```bash
.venv\Scripts\python -m unittest discover -s tests
```

These cover course parsing (section numbering, unit reviews, current unit), projections, the `data.json` shape, the HTML escaping of the inlined data, and every notification state.

## Things to know

- **Unofficial API.** Duolingo can change or block it at any time. If the script says `totalSessionTime` is missing, run with `--debug` and update the field name. Each run logs the size of both responses and raises a warning if the course call goes over 2 MB, which would mean the nested field filter stopped working.
- **Session time comes from Duolingo,** so it counts lesson time, not time spent idle in the app.
- **Nudge usage:** about 14 short runs a day.

## Ideas to extend it (good learning steps)

- Send one "🎉 goal met" message per day. ntfy can be its own state store: poll your topic (`/json?poll=1&since=12h`) to see whether today's celebration has already gone out.
- Add a weekly summary on Sunday evenings using a 7-day `xp_summaries` range.
- Build an iOS Shortcut that reads the dashboard's `data.json` (it's public and tiny) for a home-screen widget.
