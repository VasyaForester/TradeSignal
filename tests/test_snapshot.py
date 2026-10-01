import json
import re
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from scripts.update_data import (
    TelegramChannelParser,
    build_bonds_by_coupon,
    build_catalysts,
    build_dividend_calendar,
    build_high_coupon_issues,
    confirm_dividends,
    parse_snowball_calendar,
    build_market_brief,
    build_scalp_signals,
    classify_idea,
    classify_reaction,
    compute_signal_score,
    estimate_fund_return,
    estimate_stock_target,
    evaluate_entity_linking,
    event_key,
    extract_coupon_pct,
    high_coupon_floor,
    impact_estimate,
    is_bond_issue_news,
    is_macro_analyst_commentary,
    still_upcoming_dividends,
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
            "sourceHealth", "pipelineMetrics", "dividendCalendar",
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

    def test_coupon_bond_sections_when_present(self):
        if "couponBonds" not in self.data:
            self.skipTest("снимок ещё без рейтинга по купону")
        coupons = [item["coupon"] for item in self.data["couponBonds"]]
        self.assertGreater(len(coupons), 0)
        self.assertEqual(coupons, sorted(coupons, reverse=True))
        self.assertTrue(all(item["coupon"] >= 12 for item in self.data["couponBonds"]))
        self.assertIn("highCouponIssues", self.data)

    def test_dividend_calendar_when_present(self):
        if "dividendCalendar" not in self.data:
            self.skipTest("снимок ещё без календаря дивидендов")
        items = self.data["dividendCalendar"]
        self.assertLessEqual(len(items), 40)
        dates = [item["cutoffDate"] for item in items]
        self.assertEqual(dates, sorted(dates))
        for item in items:
            self.assertTrue(item.get("secid"))
            self.assertTrue(item.get("cutoffDate"))
            self.assertGreater(item.get("dividendRub") or 0, 0)
            self.assertGreater(item.get("yieldPct") or 0, 0)
            self.assertLessEqual(item.get("yieldPct") or 0, 80)
            self.assertEqual(item.get("currency"), "RUB")

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
        self.assertIsNone(
            related_instrument(
                '"Михайловский Молочный Завод" допустил техдефолт по облигациям серии 001P-01&nbsp;',
                [],
                [{"secid": "NBSP", "name": "NBSP"}],
            )
        )
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
        fitroo = (
            "Завтраки сдались без боя // Производитель хлопьев Fitroo может обанкротиться. "
            "Сбербанк намерен обратиться в арбитражный суд с заявлением о признании банкротом ООО «Фитроо». "
            "Заявление о ее банкротстве намеревается подать Сбербанк."
        )
        self.assertTrue(is_negative_actor_only(fitroo, "SBER"))
        self.assertFalse(
            is_negative_actor_only("Сбербанк может обанкротиться из-за проблем с капиталом", "SBER")
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
        market_wrap = resolve_related_instrument(
            "Рынок вновь в красном, в аутсайдерах - «Распадская» после приостановки работы шахты",
            "В минусе также Русагро и другие бумаги сырьевого сектора.",
            [{"secid": "AGRO", "name": "Русагро"}],
            [],
            [],
            "trading_halt",
        )
        self.assertIsNone(market_wrap)

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


class CouponBondTest(unittest.TestCase):
    def test_extracts_coupon_from_placement_headline(self):
        self.assertEqual(
            extract_coupon_pct("Компания разместила облигации с купоном 22,5% годовых"),
            22.5,
        )
        self.assertEqual(extract_coupon_pct("ориентир купона 19-21%", 14), 21.0)
        self.assertEqual(extract_coupon_pct("купон КС + 5%", 14.0), 19.0)
        self.assertEqual(high_coupon_floor(14), 18.0)
        self.assertIsNone(extract_coupon_pct("Росагролизинг разместил облигации на 10 млрд рублей"))

    def test_detects_new_issue_news(self):
        self.assertTrue(is_bond_issue_news("Эмитент разместил облигации серии 001P-03"))
        self.assertFalse(is_bond_issue_news("Сбер рекомендовал дивиденды"))
        self.assertFalse(is_bond_issue_news("Дефолт по облигациям серии БО-01"))

    def test_coupon_ranking_sorts_by_coupon_not_ytm(self):
        maturity = f"{date.today().year + 2}-06-01"
        board_rows = {
            "TQCB": [
                {
                    "SECID": "LOW",
                    "SHORTNAME": "Низкий купон",
                    "MATDATE": maturity,
                    "COUPONPERCENT": 13,
                    "LAST": 99,
                    "YIELD": 28,
                    "DURATION": 200,
                    "VALTODAY_RUR": 50_000,
                },
                {
                    "SECID": "HIGH",
                    "SHORTNAME": "Высокий купон",
                    "MATDATE": maturity,
                    "COUPONPERCENT": 24,
                    "LAST": 101,
                    "YIELD": 22,
                    "DURATION": 180,
                    "VALTODAY_RUR": 40_000,
                },
            ],
            "TQOB": [],
        }
        ranked = build_bonds_by_coupon(
            {"macro": {"currentKeyRate": 14, "forecastKeyRate12m": 12}},
            board_rows,
        )
        self.assertEqual([item["secid"] for item in ranked], ["HIGH", "LOW"])
        self.assertEqual(ranked[0]["coupon"], 24)

    def test_high_coupon_issues_keep_fat_coupons_only(self):
        today = date.today().isoformat()
        maturity = f"{date.today().year + 2}-06-01"
        news = [
            {
                "title": "Завод разместил облигации с купоном 21% годовых",
                "description": "Первичное размещение.",
                "source": "Финам: облигации",
                "url": "https://example.com/high",
                "publishedAt": datetime.now(timezone(timedelta(hours=3))).isoformat(),
            },
            {
                "title": "Росагролизинг разместил облигации на 10 млрд рублей",
                "description": "Без ставки купона.",
                "source": "Финам: облигации",
                "url": "https://example.com/plain",
                "publishedAt": datetime.now(timezone(timedelta(hours=3))).isoformat(),
            },
            {
                "title": "Компания разместила облигации с купоном 12% годовых",
                "description": "Слишком низко для фильтра.",
                "source": "Финам: облигации",
                "url": "https://example.com/low",
                "publishedAt": datetime.now(timezone(timedelta(hours=3))).isoformat(),
            },
        ]
        board_rows = {
            "TQCB": [{
                "SECID": "NEWHIGH",
                "SHORTNAME": "Новый высокий",
                "MATDATE": maturity,
                "COUPONPERCENT": 20,
                "LAST": 100,
                "YIELD": 20.5,
                "DURATION": 365,
                "VALTODAY_RUR": 0,
                "ISSUEDATE": today,
            }],
            "TQOB": [],
        }
        items = build_high_coupon_issues(
            news,
            board_rows,
            {"macro": {"currentKeyRate": 14, "forecastKeyRate12m": 12}},
        )
        coupons = {round(item["coupon"], 1) for item in items}
        titles = " ".join(item["title"] for item in items)
        self.assertIn(21.0, coupons)
        self.assertIn(20.0, coupons)
        self.assertNotIn(12.0, coupons)
        self.assertIn("21%", titles)
        self.assertIn("Новый высокий", titles)
        self.assertNotIn("10 млрд", titles)


class DividendCalendarTest(unittest.TestCase):
    TODAY = date(2026, 9, 8)

    def test_keeps_confirmed_rub_payout_inside_60_days(self):
        items = confirm_dividends(
            {
                "SBER": [
                    {"registryclosedate": "2026-08-01", "value": 10, "currencyid": "RUB"},
                    {"registryclosedate": "2026-09-20", "value": 10, "currencyid": "RUB"},
                    {"registryclosedate": "2026-12-01", "value": 10, "currencyid": "RUB"},
                    {"registryclosedate": "2026-09-21", "value": 5, "currencyid": "USD"},
                    {"registryclosedate": "2026-09-22", "value": 0, "currencyid": "RUB"},
                ],
            },
            {"SBER": 200},
            {"SBER": "Сбербанк"},
            today=self.TODAY,
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["secid"], "SBER")
        self.assertEqual(items[0]["cutoffDate"], "2026-09-20")
        self.assertEqual(items[0]["dividendRub"], 10)
        self.assertEqual(items[0]["yieldPct"], 5.0)
        self.assertEqual(items[0]["currency"], "RUB")

    def test_skips_absurd_yield(self):
        items = confirm_dividends(
            {"GAZP": [{"registryclosedate": "2026-09-20", "value": 200, "currencyid": "RUB"}]},
            {"GAZP": 2},
            {"GAZP": "Газпром"},
            today=self.TODAY,
        )
        self.assertEqual(items, [])

    def test_still_upcoming_drops_past_cutoffs(self):
        kept = still_upcoming_dividends(
            [
                {"secid": "SBER", "cutoffDate": "2026-09-20", "yieldPct": 5},
                {"secid": "OLD", "cutoffDate": "2026-08-01", "yieldPct": 4},
            ],
            today=self.TODAY,
        )
        self.assertEqual([item["secid"] for item in kept], ["SBER"])

    def test_build_calendar_uses_injected_iss_rows(self):
        items = build_dividend_calendar(
            {"stocks": [{"secid": "SBER", "name": "Сбербанк"}]},
            [{"secid": "SBER", "name": "Сбербанк", "price": 200}],
            previous=[{
                "secid": "GAZP",
                "cutoffDate": "2026-09-15",
                "yieldPct": 3.1,
                "dividendRub": 10,
                "currency": "RUB",
            }],
            today=self.TODAY,
            raw_by_secid={
                "SBER": [{"registryclosedate": "2026-09-20", "value": 10, "currencyid": "RUB"}],
            },
        )
        self.assertEqual([item["secid"] for item in items], ["GAZP", "SBER"])
        self.assertEqual(items[1]["yieldPct"], 5.0)

    def test_build_calendar_falls_back_to_previous_window(self):
        items = build_dividend_calendar(
            {"stocks": [{"secid": "SBER", "name": "Сбербанк"}]},
            [{"secid": "SBER", "name": "Сбербанк", "price": 200}],
            previous=[
                {
                    "secid": "GAZP",
                    "cutoffDate": "2026-09-15",
                    "yieldPct": 3.1,
                    "dividendRub": 10,
                    "currency": "RUB",
                },
                {
                    "secid": "OLD",
                    "cutoffDate": "2026-08-01",
                    "yieldPct": 4,
                    "dividendRub": 8,
                    "currency": "RUB",
                },
            ],
            today=self.TODAY,
            raw_by_secid={"SBER": []},
        )
        self.assertEqual([item["secid"] for item in items], ["GAZP"])

    def test_does_not_keep_previous_for_fetched_empty_ticker(self):
        items = build_dividend_calendar(
            {"stocks": [{"secid": "SBER", "name": "Сбербанк"}]},
            [{"secid": "SBER", "name": "Сбербанк", "price": 200}],
            previous=[{
                "secid": "SBER",
                "cutoffDate": "2026-09-18",
                "yieldPct": 4.0,
                "dividendRub": 8,
                "currency": "RUB",
            }],
            today=self.TODAY,
            raw_by_secid={"SBER": []},
        )
        self.assertEqual(items, [])

    SNOWBALL_SAMPLE = """
| Компания | Дата закрытия реестра | Купить до | На 1 акцию | Див. доходность | Статус | Частота выплат |
| --- | --- | --- | --- | --- | --- | --- |
| ЯНДЕКСYDEX | 21 сент. 26 через 13 дней | 18 сент. 26 | 110 ₽ | 2,87% | Рекомендованы | Раз в полгода |
| КуйбышевазотKAZT | 23 сент. 26 через 15 дней | 22 сент. 26 | 2,16 ₽ | 0,57% | Прогноз | Раз в полгода |
| ХэдхантерHEAD | 28 сент. 26 через 20 дней | 25 сент. 26 | 200 ₽ | 7,2% | Рекомендованы | Раз в полгода |
| НК ЛУКОЙЛLKOH | 12 дек. 26 через 3 месяца | 10 дек. 26 | 309,5 ₽ | 6,02% | Прогноз | Другое |
| РусагроRAGR | 1 авг. 26 10 дней назад | 31 июл. 26 | 16,48 ₽ | 20,9% | Объявлены | Раз в год |
"""

    def test_snowball_keeps_recommended_skips_forecasts(self):
        items = parse_snowball_calendar(self.SNOWBALL_SAMPLE, today=self.TODAY)
        self.assertEqual([item["secid"] for item in items], ["YDEX", "HEAD"])
        self.assertEqual(items[0]["cutoffDate"], "2026-09-21")
        self.assertEqual(items[0]["dividendRub"], 110)
        self.assertEqual(items[0]["yieldPct"], 2.87)
        self.assertEqual(items[0]["status"], "recommended")
        self.assertEqual(items[0]["source"]["publisher"], "Snowball Income")

    def test_build_calendar_from_snowball_html(self):
        html = """
        <table>
          <tr><th>Компания</th><th>Дата закрытия реестра</th><th>Купить до</th>
              <th>На 1 акцию</th><th>Див. доходность</th><th>Статус</th></tr>
          <tr><td>Банк Санкт-Петербург BSPB</td><td>5 окт. 26</td><td>2 окт. 26</td>
              <td>19,17 ₽</td><td>7,57%</td><td>Рекомендованы</td></tr>
          <tr><td>Селигдар SELG</td><td>12 окт. 26</td><td>9 окт. 26</td>
              <td>2 ₽</td><td>5,66%</td><td>Прогноз</td></tr>
        </table>
        """
        items = build_dividend_calendar(
            {"stocks": []},
            [],
            previous=[],
            today=self.TODAY,
            html=html,
        )
        self.assertEqual([item["secid"] for item in items], ["BSPB"])
        self.assertEqual(items[0]["yieldPct"], 7.57)
        self.assertEqual(items[0]["cutoffDate"], "2026-10-05")


if __name__ == "__main__":
    unittest.main()
