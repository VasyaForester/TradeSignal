import json
import re
import unittest
from pathlib import Path

from scripts.update_data import (
    TelegramChannelParser,
    evaluate_entity_linking,
    event_key,
    impact_estimate,
    is_mechanical_dividend_event,
    is_negative_actor_only,
    jaccard_similarity,
    related_instrument,
    text_shingles,
)


ROOT = Path(__file__).resolve().parents[1]


class SnapshotContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((ROOT / "data" / "market-data.json").read_text(encoding="utf-8"))

    def test_required_sections_exist(self):
        for key in (
            "generatedAt", "urgent", "stocks", "bonds", "funds",
            "sourceHealth", "pipelineMetrics",
        ):
            self.assertIn(key, self.data)

    def test_rankings_are_top_ten_and_sorted(self):
        for key in ("stocks", "bonds", "funds"):
            items = self.data[key]
            self.assertLessEqual(len(items), 10)
            self.assertGreater(len(items), 0, f"{key} must keep a usable snapshot")
            values = [item["expectedReturn"] for item in items]
            self.assertEqual(values, sorted(values, reverse=True))

    def test_bond_ranking_contains_corporate_issues(self):
        self.assertTrue(
            any(item["kind"] == "Корпоративная" for item in self.data["bonds"]),
            "bond ranking must not contain only OFZ",
        )

    def test_items_have_risk_and_confidence(self):
        for key in ("stocks", "bonds", "funds"):
            for item in self.data[key]:
                self.assertTrue(item["thesis"])
                self.assertTrue(item["risks"])
                self.assertGreaterEqual(item["confidence"], 0)
                self.assertLessEqual(item["confidence"], 100)

    def test_pipeline_quality_metrics(self):
        metrics = self.data["pipelineMetrics"]
        for key in ("dedupRate", "entityLinkPrecision", "entityLinkRecall", "latencyMs"):
            self.assertIn(key, metrics)
        self.assertGreaterEqual(metrics["dedupRate"], 0)
        self.assertLessEqual(metrics["dedupRate"], 1)
        evaluated = evaluate_entity_linking([], [], [{"secid": "LQDT"}])
        self.assertEqual(evaluated["entityEvalSamples"], 9)
        self.assertGreaterEqual(evaluated["entityLinkPrecision"], 0.9)
        self.assertGreaterEqual(evaluated["entityLinkRecall"], 0.9)

    def test_urgent_signals_are_attributable(self):
        self.assertLessEqual(len(self.data["urgent"]), 10)
        fingerprints = set()
        for signal in self.data["urgent"]:
            self.assertNotIn(signal["ticker"], ("РЫНОК", "НЕФТЕГАЗ"))
            self.assertTrue(signal["hashtags"])
            self.assertTrue(all(tag.startswith("#") for tag in signal["hashtags"]))
            self.assertTrue(signal["source"]["publisher"])
            self.assertTrue(signal["source"]["url"].startswith("http"))
            self.assertIn(signal["action"], ("BUY", "SELL"))
            self.assertGreaterEqual(signal["sentimentScore"], -1)
            self.assertLessEqual(signal["sentimentScore"], 1)
            self.assertGreaterEqual(signal["impactConfidence"], 0)
            self.assertLessEqual(signal["impactConfidence"], 100)
            self.assertGreaterEqual(signal["entityConfidence"], 0)
            self.assertLessEqual(signal["entityConfidence"], 100)
            normalized_title = re.sub(r"\W+", "", signal["title"].lower())
            fingerprint = (signal["ticker"], signal["action"], normalized_title)
            self.assertNotIn(fingerprint, fingerprints)
            fingerprints.add(fingerprint)

    def test_only_specific_instruments_are_detected(self):
        self.assertIsNone(related_instrument("Российский рынок сегодня снизился", [], []))
        self.assertEqual(
            related_instrument("По ценным бумагам ASTR проводится аукцион", [], [])[0],
            "ASTR",
        )
        self.assertEqual(
            related_instrument("Приостановлены облигации RU000A108FC2", [], [])[0],
            "RU000A108FC2",
        )
        self.assertEqual(
            related_instrument("🇷🇺#SBER рекомендовал дивиденды", [], [])[0],
            "SBER",
        )
        self.assertIsNone(
            related_instrument(
                "🇷🇺#LQDT показал рост",
                [],
                [],
                [{"secid": "LQDT"}],
            )
        )
        self.assertEqual(
            related_instrument(
                "🇷🇺#OZPH Озон Фармацевтика обновила дивидендную политику",
                [{"secid": "OZON", "name": "Ozon"}],
                [],
            )[0],
            "OZPH",
        )
        self.assertIsNone(related_instrument("Лента новостей: рынки снизились", [], []))
        self.assertEqual(
            related_instrument(
                "Сбербанк обсуждал с Евротрансом варианты урегулирования",
                [],
                [],
            )[0],
            "EUTR",
        )
        self.assertTrue(
            is_negative_actor_only(
                'Сбербанк намерен инициировать банкротство сети АЗС "Трасса"',
                "SBER",
            )
        )

    def test_similarity_and_impact_models(self):
        first = text_shingles("Сбербанк рекомендовал дивиденды за 2025 год")
        duplicate = text_shingles("Сбербанк рекомендовал дивиденды за 2025 год акционерам")
        unrelated = text_shingles("Лукойл сообщил о новом нефтяном месторождении")
        self.assertGreater(jaccard_similarity(first, duplicate), 0.5)
        self.assertLess(jaccard_similarity(first, unrelated), 0.2)
        positive = impact_estimate("BUY", 30, 0.9, 3)
        negative = impact_estimate("SELL", 30, 0.9, 3)
        self.assertGreater(positive[0], 0)
        self.assertGreater(positive[1], 0)
        self.assertLess(negative[0], 0)
        self.assertLess(negative[1], 0)
        self.assertEqual(event_key("эмитент подтвердил дефолт", "SELL"), "credit_distress")
        self.assertEqual(event_key("кредитор подал на банкротство", "SELL"), "credit_distress")
        self.assertTrue(is_mechanical_dividend_event("акции упали после дивидендной отсечки"))
        self.assertFalse(is_mechanical_dividend_event("акции закрыли дивидендный гэп"))

    def test_market_twits_public_page_parser(self):
        parser = TelegramChannelParser("MarketTwits")
        parser.feed(
            '<div data-post="markettwits/42">'
            '<div class="tgme_widget_message_text"><b>🇷🇺#SBER</b> рекомендовал дивиденды</div>'
            '<a class="tgme_widget_message_date" href="https://t.me/markettwits/42">'
            '<time datetime="2026-08-05T12:00:00+00:00"></time></a></div>'
        )
        items = parser.finish()
        self.assertEqual(len(items), 1)
        self.assertIn("#SBER", items[0]["description"])
        self.assertEqual(items[0]["url"], "https://t.me/markettwits/42")


if __name__ == "__main__":
    unittest.main()
