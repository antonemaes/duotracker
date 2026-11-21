# duo-nudge 🦉

Sends a push notification to your iPhone saying how many minutes of Duolingo lessons you have left today. It stays quiet once you've done 60 minutes.

```
🦉 37 min of Duolingo left
▓▓▓░░░░░░░ 38%
Done today: 23 / 60 min (6 lessons)
4.6 h until midnight
```

**Stack:** Python (standard library + `python-dotenv`/`tzdata`, see `requirements.txt`), Duolingo's *unofficial* `xp_summaries` endpoint, GitHub Actions for the schedule, and [ntfy](https://ntfy.sh) for push notifications.

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
   `.env` is gitignored — the script loads it automatically; real environment variables still take precedence (that's how GitHub Actions injects secrets).
4. **Test locally:**
   ```bash
   .venv\Scripts\python duo_nudge.py --force --debug --dry-run   # check the raw fields
   .venv\Scripts\python duo_nudge.py --force                     # send a real push
   ```
5. **Push to a *private* GitHub repo.** In Settings → Secrets and variables → Actions, add the secrets `DUO_JWT` and `NTFY_TOPIC`.
6. **Test the workflow:** go to Actions → *Duolingo nudge* → *Run workflow*.

## Tuning

Edit the `env:` block in `.github/workflows/nudge.yml`:

| Variable | Default | Meaning |
|---|---|---|
| `DUO_GOAL_MINUTES` | `60` | Daily goal |
| `DUO_NUDGE_HOURS` | `12,16,19,21,22` | Local hours to nudge. The last one is marked "last call" |
| `DUO_TZ` | `Europe/Zurich` | Your time zone |

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
