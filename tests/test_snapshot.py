import json
import re
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.update_data import (
    TelegramChannelParser,
    build_catalysts,
    build_market_brief,
    build_scalp_signals,
    classify_idea,
    classify_reaction,
    compute_signal_score,
    estimate_fund_return,
    estimate_stock_target,
    evaluate_entity_linking,
    event_key,
    impact_estimate,
    is_macro_analyst_commentary,
    is_mechanical_dividend_event,
    is_negative_actor_only,
    jaccard_similarity,
    market_stance,
    novelty_score,
    related_instrument,
    resolve_related_instrument,
    should_merge_signals,
    source_quality_score,
    text_shingles,
    update_signal_ledger,
)


ROOT = Path(__file__).resolve().parents[1]


class SnapshotContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = json.loads((ROOT / "data" / "market-data.json").read_text(encoding="utf-8"))

    def test_required_sections_exist(self):
        for key in (
            "generatedAt", "urgent", "scalp", "marketBrief", "stocks", "bonds", "funds",
            "sourceHealth", "pipelineMetrics",
        ):
            self.assertIn(key, self.data)

    def test_intelligence_sections_when_present(self):
        if "catalysts" not in self.data:
            self.skipTest("снимок ещё без слоя катализаторов")
        for key in (
            "marketTape", "catalysts", "anomalies", "sectors", "marketRegime",
            "marketPulse", "sinceLastUpdate", "signalPerformance", "drivers",
        ):
            self.assertIn(key, self.data)
        for stock in self.data["stocks"]:
            self.assertIn(stock.get("stance"), {"BUY", "WATCH", "AVOID", None})

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

    def test_scalp_signals_are_bounce_candidates(self):
        items = self.data.get("scalp") or []
        self.assertLessEqual(len(items), 8)
        seen = set()
        for signal in items:
            self.assertEqual(signal["action"], "BUY")
            self.assertTrue(signal["ticker"])
            self.assertNotIn(signal["ticker"], seen)
            seen.add(signal["ticker"])
            self.assertLessEqual(signal["dayChange"], -1.0)
            self.assertTrue(signal["summary"])
            self.assertTrue(signal["source"]["url"].startswith("http"))
            self.assertIn(signal.get("catalyst"), {
                "nonfundamental_news", "market_drawdown", "relative_weakness", "session_washout",
            })

    def test_scalp_model_skips_fundamental_distress(self):
        universe = [
            {"secid": "SBER", "name": "Сбербанк", "price": 300, "dayChange": -3.2, "liquidityRub": 8_000_000_000},
            {"secid": "GAZP", "name": "Газпром", "price": 120, "dayChange": -0.4, "liquidityRub": 4_000_000_000},
            {"secid": "YDEX", "name": "Яндекс", "price": 4000, "dayChange": -1.0, "liquidityRub": 2_000_000_000},
            {"secid": "EUTR", "name": "ЕвроТранс", "price": 90, "dayChange": -6.5, "liquidityRub": 80_000_000},
        ]
        blocked = build_scalp_signals(universe, [
            {"ticker": "EUTR", "action": "SELL", "eventType": "credit_distress", "title": "дефолт"},
        ])
        self.assertFalse(any(item["ticker"] == "EUTR" for item in blocked))
        self.assertTrue(any(item["ticker"] == "SBER" for item in blocked))

        legal = build_scalp_signals(universe, [
            {"ticker": "SBER", "action": "SELL", "eventType": "legal", "title": "Арест крупного акционера"},
        ])
        sber = next(item for item in legal if item["ticker"] == "SBER")
        self.assertEqual(sber["catalyst"], "nonfundamental_news")
        self.assertIn("Арест", sber["summary"])

    def test_market_brief_reads_index_and_avoids_chasing(self):
        self.assertEqual(market_stance(-0.9), "падает")
        self.assertEqual(market_stance(0.05), "боковик")
        self.assertEqual(market_stance(0.8), "растет")
        stocks = [
            {"secid": "SBER", "name": "Сбербанк", "expectedReturn": 18, "confidence": 70, "dayChange": -1.2},
            {"secid": "PLZL", "name": "Полюс", "expectedReturn": 14, "confidence": 65, "dayChange": -0.9},
            {"secid": "YDEX", "name": "Яндекс", "expectedReturn": 9, "confidence": 55, "dayChange": -1.0},
        ]
        falling = build_market_brief(
            {"index": "IMOEX", "indexName": "Индекс МосБиржи", "value": 2710.4, "dayChange": -0.84},
            stocks,
            [],
            {"currentKeyRate": 14.0},
        )
        self.assertEqual(falling["stance"], "падает")
        self.assertEqual(falling["horizon"], "краткосрочно")
        self.assertEqual(falling["longVerdict"], "точечно")
        self.assertEqual(falling["value"], 2710.4)
        self.assertEqual(falling["dayChange"], -0.84)
        self.assertTrue(falling["longTickers"])
        shock = build_market_brief(
            {"index": "IMOEX", "value": 2650, "dayChange": -2.5},
            stocks,
            [{"ticker": "SBER", "action": "SELL", "eventType": "sanctions", "title": "новые санкции"}],
            {"currentKeyRate": 14.0},
        )
        self.assertEqual(shock["longVerdict"], "не разгонять")
        self.assertNotIn("SBER", {item["secid"] for item in shock["longTickers"]})
        self.assertIn("санкции", shock["why"].lower())

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
        budget_forecast = (
            "🇷🇺#бюджет #россия #прогноз Сбер понизил прогноз дефицита бюджета РФ "
            "на 2026 год до 7 трлн руб с 7.5 трлн руб, ожидает цену отсечения "
            "в новом бюджетном правиле в $50 за баррель нефти"
        )
        self.assertTrue(is_macro_analyst_commentary(budget_forecast, "SBER"))
        self.assertTrue(
            is_macro_analyst_commentary(
                "ВТБ ожидает инфляцию в РФ около 6% и курс рубля 90 за доллар",
                "VTBR",
            )
        )
        self.assertFalse(
            is_macro_analyst_commentary("Сбербанк рекомендовал дивиденды", "SBER")
        )
        self.assertFalse(
            is_macro_analyst_commentary(
                "Аналитики повысили оценку Сбербанка",
                "SBER",
            )
        )
        self.assertFalse(
            is_macro_analyst_commentary("Роснефть повысила прогноз добычи", "ROSN")
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


class IntelligenceLayerTest(unittest.TestCase):
    def test_classify_reaction_underreaction_and_anomaly(self):
        self.assertEqual(classify_reaction(5.0, 0.2), "underreaction")
        self.assertEqual(classify_reaction(2.0, -1.5), "anomaly_down")
        self.assertEqual(classify_reaction(-2.0, 1.5), "anomaly_up")
        self.assertEqual(classify_reaction(2.0, 1.5), "confirmed")
        self.assertEqual(classify_reaction(2.0, 4.0), "overreaction")

    def test_build_catalysts_flags_underreaction(self):
        urgent = [{
            "ticker": "SBER",
            "title": "Сбер рекомендовал дивиденды",
            "summary": "Совет директоров",
            "action": "BUY",
            "eventType": "dividend",
            "impactEstimatePct": 4.0,
            "impactConfidence": 80,
            "sourceQuality": 1.0,
            "source": {"publisher": "Московская биржа", "url": "https://www.moex.com/"},
        }]
        stocks = [{"secid": "SBER", "name": "Сбербанк", "dayChange": 0.3}]
        items = build_catalysts(urgent, stocks)
        self.assertEqual(items[0]["reaction"], "underreaction")
        self.assertGreater(items[0]["strength"], 5)
        self.assertTrue(items[0]["official"])

    def test_update_signal_ledger_scores_hit_after_day(self):
        moscow = timezone(timedelta(hours=3))
        now = datetime(2026, 8, 18, 12, tzinfo=moscow)
        emitted = now - timedelta(hours=24)
        urgent = [{
            "ticker": "SBER",
            "action": "BUY",
            "eventType": "dividend",
            "publishedAt": emitted.isoformat(),
            "title": "дивиденды",
            "impactEstimatePct": 2,
            "impactConfidence": 80,
        }]
        ledger, _ = update_signal_ledger(
            {"signalLedger": []},
            urgent,
            [{"secid": "SBER", "price": 100}],
            now=emitted,
        )
        ledger, stats = update_signal_ledger(
            {"signalLedger": ledger},
            urgent,
            [{"secid": "SBER", "price": 103}],
            now=now,
        )
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0]["return1d"], 3.0)
        self.assertTrue(ledger[0]["hit1d"])
        self.assertEqual(stats["n"], 1)
        self.assertEqual(stats["hitRate"], 1.0)

    def test_classify_idea_buy_watch_avoid(self):
        self.assertEqual(classify_idea({"secid": "X", "expectedReturn": -2, "targetDrivers": {}}, []), "AVOID")
        self.assertEqual(classify_idea({"secid": "Y", "expectedReturn": 8, "targetDrivers": {"news": 0}}, []), "WATCH")
        self.assertEqual(
            classify_idea(
                {"secid": "SBER", "expectedReturn": 15, "targetDrivers": {"news": 1}},
                [{"ticker": "SBER", "action": "BUY", "strength": 7, "eventType": "dividend"}],
            ),
            "BUY",
        )
        self.assertEqual(
            classify_idea(
                {"secid": "GAZP", "expectedReturn": 20, "targetDrivers": {"news": 1}},
                [{"ticker": "GAZP", "action": "SELL", "strength": 8, "eventType": "sanctions"}],
            ),
            "AVOID",
        )


if __name__ == "__main__":
    unittest.main()
