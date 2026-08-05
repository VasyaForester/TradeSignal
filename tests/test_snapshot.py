import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SnapshotContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((ROOT / "data" / "market-data.json").read_text(encoding="utf-8"))

    def test_required_sections_exist(self):
        for key in ("generatedAt", "urgent", "stocks", "bonds", "funds", "sourceHealth"):
            self.assertIn(key, self.data)

    def test_rankings_are_top_five_and_sorted(self):
        for key in ("stocks", "bonds", "funds"):
            items = self.data[key]
            self.assertLessEqual(len(items), 5)
            self.assertGreater(len(items), 0, f"{key} must keep a usable snapshot")
            values = [item["expectedReturn"] for item in items]
            self.assertEqual(values, sorted(values, reverse=True))

    def test_items_have_risk_and_confidence(self):
        for key in ("stocks", "bonds", "funds"):
            for item in self.data[key]:
                self.assertTrue(item["thesis"])
                self.assertTrue(item["risks"])
                self.assertGreaterEqual(item["confidence"], 0)
                self.assertLessEqual(item["confidence"], 100)

    def test_urgent_signals_are_attributable(self):
        for signal in self.data["urgent"]:
            self.assertTrue(signal["source"]["publisher"])
            self.assertTrue(signal["source"]["url"].startswith("http"))
            self.assertIn(signal["action"], ("BUY", "SELL"))


if __name__ == "__main__":
    unittest.main()
