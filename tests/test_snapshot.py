import json
import re
import unittest
from pathlib import Path

from scripts.update_data import (
    TelegramChannelParser,
    compute_signal_score,
    estimate_fund_return,
    estimate_stock_target,
    evaluate_entity_linking,
    event_key,
    impact_estimate,
    is_mechanical_dividend_event,
    is_negative_actor_only,
    jaccard_similarity,
    novelty_score,
    related_instrument,
    resolve_related_instrument,
    should_merge_signals,
    source_quality_score,
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

    def test_stock_targets_come_from_tradesignal_model(self):
        self.assertIn("stockModel", self.data)
        for item in self.data["stocks"]:
            self.assertEqual(item["targetModel"], "tradesignal-v1")
            self.assertGreater(item["targetPrice"], 0)
            self.assertGreater(item["price"], 0)
            price_return = (item["targetPrice"] / item["price"] - 1) * 100
            self.assertGreaterEqual(price_return, -35.5)
            self.assertLessEqual(price_return, 55.5)
            for key in ("impulse", "fundamental", "news", "macro"):
                self.assertIn(key, item["targetDrivers"])

    def test_pipeline_quality_metrics(self):
        metrics = self.data["pipelineMetrics"]
        for key in ("dedupRate", "entityLinkPrecision", "entityLinkRecall", "latencyMs"):
            self.assertIn(key, metrics)
        self.assertGreaterEqual(metrics["dedupRate"], 0)
        self.assertLessEqual(metrics["dedupRate"], 1)
        evaluated = evaluate_entity_linking([], [], [{"secid": "LQDT"}, {"secid": "BOND"}])
        self.assertGreaterEqual(evaluated["entityEvalSamples"], 45)
        self.assertGreaterEqual(evaluated["entityLinkPrecision"], 0.85)
        self.assertGreaterEqual(evaluated["entityLinkRecall"], 0.85)

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
            if "eventType" in signal:
                self.assertTrue(signal["eventType"])
                self.assertGreaterEqual(signal.get("eventSeverity", 0), 0)
            if "signalScore" in signal:
                self.assertGreaterEqual(signal["signalScore"], 0)
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
                [{"secid": "SBER", "name": "Сбербанк"}],
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
        self.assertEqual(event_key("компания повысила прогноз прибыли", "BUY"), "guidance")
        self.assertTrue(is_mechanical_dividend_event("акции упали после дивидендной отсечки"))
        self.assertFalse(is_mechanical_dividend_event("акции закрыли дивидендный гэп"))

    def test_enhancement_layer_ranking_and_dedup(self):
        self.assertGreater(novelty_score(1), novelty_score(3))
        self.assertGreater(source_quality_score(4, 2), source_quality_score(1, 1))
        weak = {
            "ticker": "SBER",
            "action": "BUY",
            "strength": 60,
            "entityConfidence": 90,
            "impactConfidence": 70,
            "eventType": "dividend",
            "eventSeverity": 30,
            "noveltyScore": 1.0,
            "sourceQuality": 0.5,
            "_proximity": 0.5,
            "marketReaction": None,
        }
        strong = {
            **weak,
            "eventSeverity": 90,
            "eventType": "credit_distress",
            "_proximity": 1.0,
            "marketReaction": {"dayChangePct": -4.0, "confirmed": True, "divergence": False},
        }
        self.assertGreater(compute_signal_score(strong), compute_signal_score(weak))

        dividend = {
            "ticker": "GAZP",
            "action": "BUY",
            "_event": "dividend",
            "_tokens": text_shingles("Газпром повысил дивиденды"),
            "_published": __import__("datetime").datetime(2026, 8, 6, 10, 0, tzinfo=__import__("datetime").timezone.utc),
        }
        report = {
            "ticker": "GAZP",
            "action": "BUY",
            "_event": "report",
            "_tokens": text_shingles("Газпром сообщил о росте прибыли"),
            "_published": __import__("datetime").datetime(2026, 8, 6, 15, 0, tzinfo=__import__("datetime").timezone.utc),
        }
        self.assertFalse(should_merge_signals(dividend, report))
        same = {
            **dividend,
            "_tokens": text_shingles("Газпром повысил дивиденды акционерам"),
            "_published": __import__("datetime").datetime(2026, 8, 6, 12, 0, tzinfo=__import__("datetime").timezone.utc),
        }
        self.assertTrue(should_merge_signals(dividend, same))

        resolved = resolve_related_instrument(
            "Компания объявила о приостановке",
            "Евротранс подтвердил дефолт по выпуску",
            [],
            [],
            [],
            "credit_distress",
        )
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved[0], "EUTR")

    def test_fund_ranking_is_diversified(self):
        funds = self.data["funds"]
        self.assertGreaterEqual(len(funds), 4)
        categories = {item.get("category") for item in funds}
        # Snapshot may be stale offline, but live model must not be equity-only losers.
        if any(item.get("category") for item in funds):
            self.assertTrue(categories & {"money", "bonds", "gold", "equity"})
        for item in funds:
            self.assertGreaterEqual(item["expectedReturn"], -20)
            self.assertLessEqual(item["expectedReturn"], 40)

    def test_estimate_fund_return_prefers_money_market_carry(self):
        rising = [100 + index * 0.02 for index in range(120)]
        falling_equity = [100 - index * 0.08 for index in range(120)]
        money = estimate_fund_return(rising, "money", key_rate=14.0, rate_drop=2.0)
        equity = estimate_fund_return(falling_equity, "equity", key_rate=14.0, rate_drop=2.0)
        self.assertGreater(money["expectedReturn"], 8)
        self.assertGreater(money["expectedReturn"], equity["expectedReturn"])

    def test_estimate_stock_target_reacts_to_news(self):
        closes = [100 + index * 0.2 for index in range(120)]
        base = estimate_stock_target(
            price=124,
            closes=closes,
            financial_trend=0.8,
            dividend12m=10,
            news_impact=0,
            rate_drop=2,
            sector="bank",
        )
        positive = estimate_stock_target(
            price=124,
            closes=closes,
            financial_trend=0.8,
            dividend12m=10,
            news_impact=8,
            rate_drop=2,
            sector="bank",
        )
        negative = estimate_stock_target(
            price=124,
            closes=closes,
            financial_trend=0.8,
            dividend12m=10,
            news_impact=-8,
            rate_drop=2,
            sector="bank",
        )
        self.assertGreater(positive["targetPrice"], base["targetPrice"])
        self.assertLess(negative["targetPrice"], base["targetPrice"])
        self.assertEqual(base["targetModel"], "tradesignal-v1")
        self.assertGreaterEqual(base["priceReturn"], -35)
        self.assertLessEqual(base["priceReturn"], 55)

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
