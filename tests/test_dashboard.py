"""Tests for duo_dashboard.py. Run: python -m unittest discover -s tests"""
import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import duo_dashboard as dd  # noqa: E402

TZ = ZoneInfo("Europe/Zurich")


def load(name):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def cfg(**over):
    base = {
        "tz": TZ, "days": 150, "chart_start": date(2026, 10, 1), "daily_target": 25,
        "weekly_target": 175, "goal_minutes": 60, "proj_groups": dd.parse_groups("B1=5,6;B2=7,8"),
        "repo": "owner/repo",
    }
    base.update(over)
    return base


class ParseCourse(unittest.TestCase):
    def setUp(self):
        self.course = dd.parse_course(load("mock_course.json"))

    def test_sections_numbered_like_the_app(self):
        # API index 0 is the app's "Section 1"; the daily_refresh pseudo-section is skipped
        self.assertEqual([s["n"] for s in self.course["sections"]], [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(self.course["sections"][0]["cefr"], "Intro")
        self.assertEqual(self.course["sections"][4]["cefr"], "B1")

    def test_unit_reviews_excluded_and_passed_levels_done(self):
        s1 = self.course["sections"][0]          # 10 finished units, 12 sessions each without the review
        self.assertEqual(s1["total"], 10 * 12)
        self.assertEqual(s1["done"], s1["total"])
        self.assertEqual(s1["unitsDone"], 10)

    def test_current_unit_comes_from_the_active_level(self):
        cur = self.course["current"]
        self.assertEqual((cur["section"], cur["unit"], cur["units"]), (5, 9, 40))
        self.assertEqual(cur["cefr"], "B1")
        self.assertTrue(self.course["sections"][4]["active"])
        self.assertFalse(self.course["sections"][3]["active"])

    def test_old_shape_still_parses(self):
        old = {"currentCourse": {"pathSectioned": [
            {"units": [{"steps": [{"totalSessions": 4, "finishedSessions": 4}]}]},
            {"units": [{"steps": [{"totalSessions": 4, "finishedSessions": 1}]}]},
        ]}}
        c = dd.parse_course(old)
        self.assertEqual([(s["n"], s["total"], s["done"]) for s in c["sections"]], [(1, 4, 4), (2, 4, 1)])
        self.assertEqual(c["current"]["section"], 2)


class Projections(unittest.TestCase):
    DAY = date(2026, 11, 22)

    def sections(self):
        return [
            {"n": 4, "cefr": "A2", "total": 100, "done": 100},
            {"n": 5, "cefr": "B1", "total": 100, "done": 40},
            {"n": 6, "cefr": "B1", "total": 100, "done": 0},
            {"n": 7, "cefr": "B2", "total": 50, "done": 0},
            {"n": 8, "cefr": "B2", "total": 50, "done": 0},
        ]

    def test_parse_groups(self):
        self.assertEqual(dd.parse_groups("B1=5,6;B2=7,8"),
                         [{"name": "B1", "sections": [5, 6]}, {"name": "B2", "sections": [7, 8]}])
        # the old DUO_PROJECTION_SECTIONS format: one card per section
        self.assertEqual(dd.parse_groups("5,6"),
                         [{"name": "Section 5", "sections": [5]}, {"name": "Section 6", "sections": [6]}])

    def test_level_finishes_with_its_last_section(self):
        b1, b2 = dd.project_groups(self.sections(), self.DAY, 10, 25, dd.parse_groups("B1=5,6;B2=7,8"))
        self.assertEqual(b1["label"], "B1 level")
        self.assertEqual((b1["total"], b1["done"], b1["remaining"]), (200, 40, 160))
        self.assertEqual((b1["through"], b1["days"], b1["date"]), (160, 16, "2026-12-08"))
        self.assertEqual(b1["targetDays"], 7)               # ceil(160 / 25)
        self.assertEqual((b2["remaining"], b2["through"], b2["days"]), (100, 260, 26))

    def test_finished_missing_and_no_rate(self):
        a2, c1 = dd.project_groups(self.sections(), self.DAY, 10, 25, dd.parse_groups("A2=4;C1=9,10"))
        self.assertTrue(a2["finished"])
        self.assertTrue(c1["missing"])
        (b1,) = dd.project_groups(self.sections(), self.DAY, 0, 25, dd.parse_groups("B1=5,6"))
        self.assertNotIn("date", b1)
        self.assertIn("targetDate", b1)

    def test_every_unfinished_section_gets_an_eta(self):
        secs = dd.section_etas(self.sections(), self.DAY, 10)
        self.assertNotIn("eta", secs[0])                    # finished
        self.assertEqual([s.get("eta") for s in secs[1:]],
                         ["2026-11-28", "2026-12-08", "2026-12-13", "2026-12-18"])


class BuildData(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 20, 18, 30, tzinfo=TZ)
        self.data = dd.build_data(cfg(), self.now, load("mock_range.json"), load("mock_course.json"))

    def test_shape(self):
        d = self.data
        self.assertEqual(d["today"], "2026-11-20")
        self.assertEqual(len(d["history"]["lessons"]), 150)
        self.assertEqual(d["course"], {"language": "German", "code": "DE", "from": "English"})
        self.assertEqual(d["streak"], {"current": 47, "longest": 282})
        self.assertEqual(d["chartStart"], "2026-10-01")
        self.assertEqual(d["path"]["current"]["section"], 5)
        self.assertEqual([(p["name"], p["sections"]) for p in d["projections"]], [("B1", [5, 6]), ("B2", [7, 8])])

    def test_rate_uses_the_last_seven_full_days(self):
        lessons = self.data["history"]["lessons"]
        self.assertAlmostEqual(self.data["stats"]["rate7"], round(sum(lessons[-8:-1]) / 7, 2))
        self.assertEqual(self.data["todayStats"]["lessons"], lessons[-1])

    def test_small_enough_for_a_5_minute_poll(self):
        self.assertLess(len(json.dumps(self.data)), 20_000)


class WriteSite(unittest.TestCase):
    def test_seed_cannot_close_the_script_tag(self):
        data = {"course": {"language": "</script><script>alert(1)</script>"}}
        with tempfile.TemporaryDirectory() as tmp:
            page, data_file = dd.write_site(Path(tmp), data, "https://example.test/")
            html = page.read_text(encoding="utf-8")
            self.assertNotIn("</script><script>alert(1)", html)
            self.assertNotIn("__DUO_SEED__", html)
            self.assertNotIn("__PAGE_URL__", html)
            self.assertIn('content="https://example.test/og.png"', html)
            self.assertEqual(json.loads(data_file.read_text(encoding="utf-8")), data)
            for asset in ("manifest.webmanifest", "icon.svg", "icon-180.png", "og.png"):
                self.assertTrue((Path(tmp) / asset).exists(), asset)


if __name__ == "__main__":
    unittest.main()
