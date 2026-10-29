import copy
import re
import unittest
from datetime import date
from pathlib import Path

import generate


class StatsTests(unittest.TestCase):
    def setUp(self):
        self.cfg = generate.read_config(Path(__file__).with_name("config.json"))
        self.today = date(2026, 10, 28)

    def payload(self, records):
        return {"schema_version": 1, "timezone": "America/New_York", "records": records}

    def test_excluded_activities_and_notes_never_reach_public_page(self):
        payload = self.payload([
            {"date": "2026-10-27", "activity": "deep work", "minutes": 120},
            {"date": "2026-10-27", "activity": "Reading", "minutes": 45},
            {"date": "2026-10-27", "activity": "Sleep", "minutes": 480,
             "note": "SECRET_NOTE"},
            {"date": "2026-10-27", "activity": "Unlisted secret activity", "minutes": 99},
        ])
        page = generate.render(self.cfg, payload, self.today)
        for private in ("Sleep", "SECRET_NOTE", "Unlisted secret activity"):
            self.assertNotIn(private, page)
        self.assertIn("Total: 2h 45m", page)

    def test_enable_sleep_fits_a_full_day(self):
        cfg = copy.deepcopy(self.cfg)
        next(a for a in cfg["activities"] if a["name"] == "Sleep")["include"] = True
        page = generate.render(cfg, self.payload([
            {"date": "2026-10-27", "activity": "Deep Work", "minutes": 960},
            {"date": "2026-10-27", "activity": "Sleep", "minutes": 480},
        ]), self.today)
        row = next(row for row in re.findall(r'<tr class="day".*?</tr>', page)
                   if 'datetime="2026-10-27"' in row)
        self.assertEqual(row.count('class="block"'), 48)
        widths = [float(n) for n in re.findall(r'width:([\d.]+)%', row)]
        self.assertAlmostEqual(sum(widths), 4800)
        self.assertIn("Total: 24h", row)
        self.assertIn("Sleep: 8h", row)

    def test_partial_block_preserves_short_sessions(self):
        markup = generate.blocks([17, 13], 30, 24)
        widths = [float(n) for n in re.findall(r'width:([\d.]+)%', markup)]
        self.assertAlmostEqual(sum(widths), 100, places=5)
        self.assertIn("c0", markup)
        self.assertIn("c1", markup)
        self.assertEqual(markup.count('class="block"'), 24)

    def test_today_and_future_records_excluded_when_configured(self):
        self.cfg["include_today"] = False
        payload = self.payload([
            {"date": "2026-10-27", "activity": "Deep Work", "minutes": 60},
            {"date": "2026-10-28", "activity": "Deep Work", "minutes": 120},
            {"date": "2026-10-29", "activity": "Deep Work", "minutes": 180},
        ])
        page = generate.render(self.cfg, payload, self.today)
        self.assertIn("Total: 1h", page)
        self.assertNotIn('datetime="2026-10-28"', page)

    def test_missing_data_never_becomes_fake_zero_history(self):
        page = generate.render(self.cfg, None, self.today)
        self.assertIn("No time data published yet.", page)
        self.assertNotIn('class="day"', page)


if __name__ == "__main__":
    unittest.main()
