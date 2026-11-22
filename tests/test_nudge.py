"""Tests for duo_nudge.py. Run: python -m unittest discover -s tests"""
import json
import sys
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import duo_nudge as dn  # noqa: E402

TZ = ZoneInfo("Europe/Zurich")


def at(hour, minute=7):
    return datetime(2026, 11, 22, hour, minute, tzinfo=TZ)


HOURS = [12, 16, 19, 21, 22]   # the workflow's DUO_NUDGE_HOURS


class PaceTarget(unittest.TestCase):
    def test_steady_pace_from_first_to_last_nudge_hour(self):
        self.assertEqual(dn.pace_target(60, at(10, 0), 12, 22), 0)    # before the window
        self.assertEqual(dn.pace_target(60, at(12, 0), 12, 22), 0)
        self.assertEqual(dn.pace_target(60, at(17, 0), 12, 22), 30)
        self.assertEqual(dn.pace_target(60, at(22, 0), 12, 22), 60)
        self.assertEqual(dn.pace_target(60, at(23, 0), 12, 22), 60)   # after it
        self.assertEqual(dn.pace_target(60, at(16, 0), 16, 16), 60)   # a single nudge hour


class BuildMessage(unittest.TestCase):
    def test_on_pace(self):
        title, body, prio = dn.build_message(23, 6, 60, at(12), HOURS, streak=47, extended=True)
        self.assertEqual(title, "🦉 37 min to go · 23 of 60 done")
        self.assertEqual(body, "6 lessons so far · 🔥 47-day streak\nAbout 10 more lessons · 11 h 53 min left today.")
        self.assertEqual(prio, "default")

    def test_a_little_behind_still_counts_as_on_pace(self):
        # 16:07: a steady pace is at ~25 min; 18 min is within the 10-minute slack
        title, _, prio = dn.build_message(18, 4, 60, at(16), HOURS)
        self.assertEqual(title, "🦉 42 min to go · 18 of 60 done")
        self.assertEqual(prio, "default")

    def test_not_started_at_the_first_nudge(self):
        title, body, prio = dn.build_message(0, 0, 60, at(12), HOURS, streak=47)
        self.assertEqual(title, "🦉 Time for today's 60 min")
        self.assertIn("streak on the line", body)
        self.assertEqual(prio, "default")

    def test_behind_pace(self):
        # 19:07: a steady pace is at ~43 min; 20 min is 23 behind
        title, body, prio = dn.build_message(20, 4, 60, at(19), HOURS)
        self.assertEqual(title, "⚠️ Behind pace: 40 min to go")
        self.assertEqual(body, "20 of 60 min · 4 lessons so far.\n"
                               "Steady pace by now: 43 min. About 8 more lessons, start now.")
        self.assertEqual(prio, "high")
        _, body, _ = dn.build_message(5, 1, 60, at(21), HOURS)
        self.assertIn("1 lesson so far", body)          # singular

    def test_nothing_done_by_the_afternoon_is_behind_pace(self):
        title, body, prio = dn.build_message(0, 0, 60, at(16), HOURS, streak=47)
        self.assertEqual(title, "⚠️ Behind pace: 60 min to go")
        self.assertEqual(body, "No lessons yet today.\nSteady pace by now: 25 min. Start a lesson now.")
        self.assertEqual(prio, "high")

    def test_every_nudge_hour_can_say_behind_pace_except_the_first(self):
        states = [dn.build_message(0, 0, 60, at(h), HOURS)[0].split(" ")[1] for h in HOURS]
        self.assertEqual(states, ["Time", "Behind", "Behind", "Behind", "Last"])

    def test_last_call_mentions_an_unextended_streak(self):
        title, body, prio = dn.build_message(0, 0, 60, at(22), HOURS, streak=47, extended=False)
        self.assertEqual(title, "🚨 Last call: 60 min to go")
        self.assertIn("keep your 47-day streak", body)
        self.assertEqual(prio, "high")
        _, body, _ = dn.build_message(30, 6, 60, at(22), HOURS, streak=47, extended=True)
        self.assertNotIn("keep your", body)

    def test_one_emoji_per_title_and_short_lines(self):
        for args in [(23, 6, 60, at(12)), (0, 0, 60, at(16)), (5, 1, 60, at(21, 55)), (0, 0, 60, at(22)),
                     (5, 1, 60, at(21, 30)), (50, 10, 60, at(21)), (0, 0, 60, at(12))]:
            title, body, _ = dn.build_message(*args, HOURS, streak=47)
            self.assertEqual(sum(ch in "🦉⚠🚨" for ch in title), 1, title)
            self.assertLessEqual(len(title), 40, title)
            self.assertLessEqual(len(body.splitlines()), 2, body)

    def test_fmt_hours(self):
        self.assertEqual(dn.fmt_hours(4.6), "4 h 36 min")
        self.assertEqual(dn.fmt_hours(0.75), "45 min")


class TodaySummary(unittest.TestCase):
    def test_mock_today(self):
        data = json.loads((HERE / "mock_today.json").read_text(encoding="utf-8"))
        day = datetime.fromtimestamp(data["summaries"][0]["date"], ZoneInfo("UTC")).date()
        self.assertEqual(dn.today_summary(data, day, TZ), (23.0, 6, True))
        self.assertEqual(dn.minutes_today(data, day, TZ), (23.0, 6))
        self.assertEqual(dn.today_summary(data, date(2000, 1, 1), TZ), (0.0, 0, False))


if __name__ == "__main__":
    unittest.main()
