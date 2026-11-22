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


class BuildMessage(unittest.TestCase):
    def test_on_pace(self):
        title, body, prio = dn.build_message(23, 6, 60, at(12), 22, streak=47, extended=True)
        self.assertEqual(title, "🦉 37 min to go · 23 of 60 done")
        self.assertEqual(body, "6 lessons so far · 🔥 47-day streak\nAbout 10 more lessons · 11 h 53 min left today.")
        self.assertEqual(prio, "default")

    def test_not_started(self):
        title, body, prio = dn.build_message(0, 0, 60, at(16), 22, streak=47)
        self.assertEqual(title, "🦉 Time for today's 60 min")
        self.assertIn("streak on the line", body)
        self.assertEqual(prio, "default")

    def test_behind_pace(self):
        title, body, prio = dn.build_message(5, 1, 60, at(22, 30), 23)   # 1.5 h left, 55 min to go
        self.assertTrue(title.startswith("⚠️ Behind pace: 55 min to go"))
        self.assertIn("1 lesson so far", body)          # singular
        self.assertEqual(prio, "high")

    def test_last_call_mentions_an_unextended_streak(self):
        title, body, prio = dn.build_message(0, 0, 60, at(22), 22, streak=47, extended=False)
        self.assertEqual(title, "🚨 Last call: 60 min to go")
        self.assertIn("keep your 47-day streak", body)
        self.assertEqual(prio, "high")
        _, body, _ = dn.build_message(30, 6, 60, at(22), 22, streak=47, extended=True)
        self.assertNotIn("keep your", body)

    def test_one_emoji_per_title_and_short_lines(self):
        for args in [(23, 6, 60, at(12)), (0, 0, 60, at(16)), (5, 1, 60, at(21, 55)), (0, 0, 60, at(22)), (5, 1, 60, at(21, 30))]:
            title, body, _ = dn.build_message(*args, 22, streak=47)
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
