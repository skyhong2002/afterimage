"""Unit tests for the pure helpers in scripts/discover.py. Run: uv run python -m unittest discover -s tests"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from discover import query_variants, too_generic, usable_ratio  # noqa: E402


def hit(res: str) -> dict:
    return {"resolution": res}


class Helpers(unittest.TestCase):
    def test_ratio_desktop(self):
        for ok in ("1920x1080", "2560x1080", "1920x1200", "3840x2160"):
            self.assertTrue(usable_ratio(hit(ok), "desktop"), ok)
        for bad in ("8102x2018", "1080x1920", "1000x1000"):
            self.assertFalse(usable_ratio(hit(bad), "desktop"), bad)

    def test_ratio_phone(self):
        self.assertTrue(usable_ratio(hit("1080x1920"), "phone"))
        self.assertFalse(usable_ratio(hit("1920x1080"), "phone"))

    def test_query_variants(self):
        self.assertEqual(query_variants("Final Fantasy XIV: Endwalker"), ["Final Fantasy XIV Endwalker", "Final Fantasy XIV"])
        self.assertEqual(query_variants("Hades"), ["Hades"])
        self.assertEqual(query_variants("Fate/Hollow Ataraxia"), ["Fate Hollow Ataraxia"])

    def test_generic_depends_on_kind(self):
        self.assertTrue(too_generic("Escape", "movie"))
        self.assertTrue(too_generic("Dark", "show"))
        self.assertFalse(too_generic("Celeste", "game"))
        # Deliberately conservative: a 4-letter single word cannot be told apart from a common
        # word, so it is skipped (search it with --work) rather than flooding the inbox.
        self.assertTrue(too_generic("Ajin", "anime"))
        self.assertFalse(too_generic("Dark Matter", "show"))
        self.assertFalse(too_generic("Escape Room Movie", "movie"))


if __name__ == "__main__":
    unittest.main()
