"""Render the fixture in-process (inside the base image: PYTHONPATH=.:/opt/slowapp)."""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from PIL import Image

from tools import reminders

HERE = Path(__file__).resolve().parent
FIXTURE = HERE.parent / "fixtures" / "two-due" / "reminders.json"
INKS = {(0, 0, 0), (255, 255, 255), (255, 255, 0), (255, 0, 0)}


class Render(unittest.TestCase):
    def test_fixture_prompts_and_bindings(self):
        with tempfile.TemporaryDirectory() as out:
            doc = reminders.render(out, now=datetime(2026, 10, 6, 10, 0), path=str(FIXTURE))
            self.assertEqual(doc["surface"], "452x444.k4")
            self.assertTrue(doc["prompts"], "something is due at 10:00")
            self.assertEqual(json.loads((Path(out) / "prompts.json").read_text()), doc)
            for p in doc["prompts"]:
                self.assertEqual(p["ask"], f"prompts/{p['id']}/ask.png")
                ask = Image.open(Path(out) / p["ask"]).convert("RGB")
                self.assertEqual(ask.size, (452, 444))
                self.assertLessEqual({c for _, c in ask.getcolors(1 << 16)}, INKS)
                self.assertEqual(set(p["bindings"]), {"own.tap", "spare.tap", "own.hold", "spare.hold"})
                for b in p["bindings"].values():
                    img = Image.open(Path(out) / b["show"]).convert("RGB")
                    self.assertEqual(img.size, (452, 444))
                    self.assertLessEqual({c for _, c in img.getcolors(1 << 16)}, INKS)

    def test_missing_file_is_no_reminders(self):
        with tempfile.TemporaryDirectory() as out:
            doc = reminders.render(out, now=datetime(2026, 10, 6, 10, 0), path=str(Path(out) / "none.json"))
            self.assertEqual(doc["prompts"], [])


if __name__ == "__main__":
    unittest.main()
