#!/usr/bin/env python3
"""Build the static market snapshot from public, attributable sources."""

from __future__ import annotations

import json
import math
import re
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "forecasts.json"
ENTITY_PATH = ROOT / "config" / "entities.json"
ENTITY_EVAL_PATH = ROOT / "config" / "entity-eval.json"
OUTPUT_PATH = ROOT / "data" / "market-data.json"
USER_AGENT = "TradeSignal/1.0 (+https://github.com/VasyaForester/TradeSignal)"
MOSCOW_TZ = timezone(timedelta(hours=3))

MOEX = "https://iss.moex.com/iss"
RANKING_LIMIT = 10
URGENT_LIMIT = 10
SCALP_LIMIT = 8
FUNDAMENTAL_SCALP_BLOCK = {
    "credit_distress",
    "license_revocation",
    "sanctions",
    "trading_halt",
    "accident",
    "guidance",
    "report",
}
NONFUNDAMENTAL_EVENTS = {"legal", "other"}
RSS_FEEDS = [
    ("Банк России", "https://www.cbr.ru/rss/RssPress"),
    ("Интерфакс", "https://www.interfax.ru/rss"),
    ("Коммерсантъ", "https://www.kommersant.ru/RSS/main.xml"),
    ("Ведомости", "https://www.vedomosti.ru/rss/news"),
    ("РБК", "https://rssexport.rbc.ru/rbcnews/news/30/full.rss"),
    ("Финам: компании", "https://www.finam.ru/analysis/conews/rsspoint/"),
    ("Финам: облигации", "https://bonds.finam.ru/news/today/rss.asp"),
    ("Investing.com", "https://ru.investing.com/rss/news_25.rss"),
    ("Газпром", "https://www.gazprom.ru/rss/"),
    ("Роснефть", "https://www.rosneft.ru/press/releases/rss/"),
]
TELEGRAM_FEEDS = [("MarketTwits", "https://t.me/s/markettwits")]
ENTITY_REGISTRY = json.loads(ENTITY_PATH.read_text(encoding="utf-8"))
ENTITY_BY_SECID = {
    item["secid"]: item
    for item in ENTITY_REGISTRY["entities"]
}
ISSUER_TICKERS = {
    alias.lower(): item["secid"]
    for item in ENTITY_REGISTRY["entities"]
    for alias in [item["name"], *item.get("aliases", [])]
}
NON_TICKER_TOKENS = {"MOEX", "RUB", "USD", "CNY", "IFRS", "BRICS", "EBITDA", "OIBDA"}
AMBIGUOUS_ALIASES = {"лента", "полюс", "астра"}
SOURCE_PRIORITY = {
    "Московская биржа": 4,
    "Банк России": 4,
    "Газпром": 4,
    "Роснефть": 4,
    "Интерфакс": 2,
    "Коммерсантъ": 2,
    "Ведомости": 2,
    "РБК": 2,
    "Финам: компании": 2,
    "Финам: облигации": 2,
    "Investing.com": 1,
    "MarketTwits": 1,
}

POSITIVE_WORDS = {
    "дивиденд": 18, "рекомендовал выплат": 28, "выкуп": 24, "байбэк": 24,
    "сильнее ожиданий": 26, "рекордн": 16, "повысил прогноз": 25,
    "снизить ключевую ставку": 20, "возобнов": 14, "рост выручк": 16,
    "рост прибыли": 18, "увеличил прибыль": 18, "повысил рейтинг": 20,
    "одобрил сделк": 16, "заключил соглашение": 14, "новый контракт": 16,
    "начал производство": 16, "разместил облигац": 14,
    "увеличил производство": 16,
}
NEGATIVE_WORDS = {
    "дефолт": 45, "банкрот": 42, "арест": 38, "обыск": 35,
    "приостанов": 25, "отказ от дивиденд": 30, "убыток": 18,
    "слабее ожиданий": 24, "понизил прогноз": 25, "дискретный аукцион": 22,
    "нарушен": 18, "санкци": 20, "авар": 24, "снижение прибыли": 20,
    "падение прибыли": 22, "падение выручки": 20, "сократил прогноз": 24,
    "отзыв лицензии": 38, "расследован": 24, "задержан": 30,
    "обвинен": 28, "пожар": 24, "прекращение торгов": 28,
    "исключении ценных бумаг": 22, "дополнительные меры": 16,
    "обвал": 60, "рухнул": 48,
}

# Additive taxonomy for ranking/dedup. Old strength/keyword detector stays intact.
EVENT_TAXONOMY = {
    "credit_distress": ("дефолт", "банкрот", "реструктуризац", "просроч"),
    "license_revocation": ("отзыв лицензии",),
    "sanctions": ("санкци",),
    "trading_halt": ("прекращение торгов", "дискретный аукцион", "приостанов"),
    "accident": ("авар", "пожар"),
    "legal": ("арест", "обыск", "расследован", "задержан", "обвинен"),
    "guidance": (
        "повысил прогноз", "понизил прогноз", "сократил прогноз",
        "повысила прогноз", "понизила прогноз", "сократила прогноз",
        "profit warning",
    ),
    "rating": ("повысил рейтинг", "понизил рейтинг"),
    "contract": ("новый контракт", "заключил соглашение"),
    "production": ("начал производство", "увеличил производство"),
    "buyback": ("выкуп", "байбэк"),
    "dividend": ("дивиденд", "рекомендовал выплат", "отказ от дивиденд"),
    "report": ("рост прибыли", "увеличил прибыль", "снижение прибыли", "падение прибыли",
               "рост выручк", "падение выручки", "сильнее ожиданий", "слабее ожиданий", "отчет"),
    "corporate_action": ("сделк", "оферт", "поглощен"),
}

EVENT_SEVERITY = {
    "credit_distress": 90,
    "license_revocation": 90,
    "sanctions": 80,
    "trading_halt": 75,
    "accident": 70,
    "legal": 55,
    "guidance": 55,
    "rating": 50,
    "buyback": 40,
    "contract": 35,
    "production": 35,
    "dividend": 30,
    "corporate_action": 35,
    "report": 25,
    "other": 20,
}

WEAK_EVENT_TYPES = {"other", "buy", "sell"}


def now_iso() -> str:
    return datetime.now(MOSCOW_TZ).replace(microsecond=0).isoformat()


def request_bytes(url: str, attempts: int = 2, timeout: int = 8) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.8 * (attempt + 1))
    raise RuntimeError(f"Не удалось загрузить {url}: {last_error}")


def get_json(url: str) -> dict[str, Any]:
    return json.loads(request_bytes(url).decode("utf-8"))


def rows(payload: dict[str, Any], block: str) -> list[dict[str, Any]]:
    section = payload.get(block, {})
    columns = section.get("columns", [])
    return [dict(zip(columns, item)) for item in section.get("data", [])]


def number(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def market_price(security: dict[str, Any], market: dict[str, Any]) -> float:
    for field in ("LAST", "MARKETPRICE", "LCLOSEPRICE", "LEGALCLOSEPRICE"):
        value = number(market.get(field))
        if value > 0:
            return value
    return number(security.get("PREVPRICE"))


def fetch_board(market: str, board: str, securities: list[str] | None = None) -> list[dict[str, Any]]:
    page_size = 100 if securities else 500
    max_pages = 1 if securities else 4
    query = {
        "iss.meta": "off",
        "iss.only": "securities,marketdata",
        "securities.columns": (
            "SECID,SHORTNAME,SECNAME,PREVPRICE,MATDATE,COUPONPERCENT,"
            "INSTRID,SECTYPE"
        ),
        "marketdata.columns": (
            "SECID,LAST,MARKETPRICE,LCLOSEPRICE,LASTTOPREVPRICE,"
            "VALTODAY,VALTODAY_RUR,YIELD,EFFECTIVEYIELD,DURATION"
        ),
        "limit": page_size,
    }
    if securities:
        query["securities"] = ",".join(securities)
    static: dict[str, dict[str, Any]] = {}
    dynamic: dict[str, dict[str, Any]] = {}
    start = 0
    for _ in range(max_pages):
        page_query = {**query, "start": start}
        url = (
            f"{MOEX}/engines/stock/markets/{market}/boards/{board}/securities.json?"
            + urllib.parse.urlencode(page_query)
        )
        payload = get_json(url)
        static_page = rows(payload, "securities")
        dynamic_page = rows(payload, "marketdata")
        static.update({item["SECID"]: item for item in static_page})
        dynamic.update({item["SECID"]: item for item in dynamic_page})
        if securities or max(len(static_page), len(dynamic_page)) < page_size:
            break
        start += page_size
    return [{**item, **dynamic.get(secid, {})} for secid, item in static.items()]


def fetch_imoex() -> dict[str, Any]:
    url = (
        f"{MOEX}/engines/stock/markets/index/securities/IMOEX.json?"
        + urllib.parse.urlencode({
            "iss.meta": "off",
            "iss.only": "securities,marketdata",
            "securities.columns": "SECID,SHORTNAME,PREVPRICE",
            "marketdata.columns": (
                "SECID,CURRENTVALUE,LASTVALUE,LASTCHANGEPRC,OPENVALUE,UPDATETIME"
            ),
        })
    )
    payload = get_json(url)
    static = next((item for item in rows(payload, "securities") if item.get("SECID") == "IMOEX"), {})
    dynamic = next((item for item in rows(payload, "marketdata") if item.get("SECID") == "IMOEX"), {})
    item = {**static, **dynamic}
    value = number(item.get("CURRENTVALUE") or item.get("LASTVALUE") or item.get("PREVPRICE"))
    prev = number(item.get("PREVPRICE") or item.get("OPENVALUE"))
    change = number(item.get("LASTCHANGEPRC"))
    if abs(change) < 0.001 and prev > 0 and value > 0:
        change = (value / prev - 1) * 100
    if value <= 0:
        raise RuntimeError("IMOEX не вернул текущее значение")
    return {
        "index": "IMOEX",
        "indexName": str(item.get("SHORTNAME") or "Индекс МосБиржи"),
        "value": round(value, 2),
        "dayChange": round(change, 2),
        "source": {
            "publisher": "Московская биржа",
            "url": "https://www.moex.com/ru/index/IMOEX",
        },
    }


SECTOR_RATE_WEIGHT = {
    "bank": 1.25,
    "tech": 0.55,
    "commodity": 0.4,
    "retail": 0.85,
    "telecom": 0.95,
    "infra": 0.75,
    "health": 0.65,
}

STOCK_MODEL = (
    "Таргет TradeSignal = текущая цена × (1 + сценарный рост). "
    "Рост = импульс 3/12 мес. + качество бизнеса + свежие новостные сигналы + чувствительность к ставке. "
    "Полная ожидаемая доходность = рост цены + прогнозный дивиденд. Clamp роста −35%…+55%."
)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def news_impact_map(signals: list[dict[str, Any]]) -> dict[str, float]:
    impacts: dict[str, float] = {}
    now = datetime.now(MOSCOW_TZ)
    for signal in signals:
        ticker = str(signal.get("ticker") or "")
        if not ticker:
            continue
        try:
            published = published_datetime(str(signal.get("publishedAt") or ""))
            age_hours = max(0.0, (now - published).total_seconds() / 3600)
        except (TypeError, ValueError, OverflowError):
            age_hours = 24.0
        if age_hours > 36:
            continue
        decay = max(0.25, 1.0 - age_hours / 48.0)
        strength = number(signal.get("impactEstimatePct"))
        if strength == 0:
            strength = number(signal.get("sentimentScore")) * 3.0
        impacts[ticker] = impacts.get(ticker, 0.0) + strength * decay
    return {ticker: clamp(value, -12.0, 12.0) for ticker, value in impacts.items()}


def estimate_stock_target(
    price: float,
    closes: list[float],
    financial_trend: float,
    dividend12m: float,
    news_impact: float,
    rate_drop: float,
    sector: str = "",
    override_target: float | None = None,
) -> dict[str, Any]:
    if override_target and override_target > 0 and price > 0:
        price_return = (override_target / price - 1) * 100
        dividend_yield = dividend12m / price * 100 if price > 0 else 0.0
        return {
            "targetPrice": round(override_target, 2),
            "priceReturn": round(price_return, 1),
            "dividendYield": round(dividend_yield, 1),
            "expectedReturn": round(price_return + dividend_yield, 1),
            "confidence": 55,
            "targetModel": "override",
            "targetDrivers": {
                "impulse": 0.0,
                "fundamental": 0.0,
                "news": 0.0,
                "macro": 0.0,
            },
        }

    ret_3m = 0.0
    ret_12m = 0.0
    if len(closes) >= 45 and closes[-1] > 0:
        ret_3m = (closes[-1] / closes[-min(63, len(closes))] - 1) * 100
        ret_12m = (closes[-1] / closes[-min(252, len(closes))] - 1) * 100

    blended_impulse = 0.55 * clamp(ret_3m, -40, 40) + 0.45 * clamp(ret_12m, -55, 55)
    # Strong past run reduces forward upside; deep drawdown adds mean-reversion lift.
    impulse = clamp(blended_impulse * 0.18 - max(0.0, blended_impulse - 25) * 0.08, -18, 22)
    fundamental = (clamp(financial_trend, 0, 1) - 0.5) * 28
    news = clamp(news_impact, -12, 12)
    macro = clamp(rate_drop, -2, 6) * SECTOR_RATE_WEIGHT.get(sector, 0.7) * 1.4
    price_return = clamp(impulse + fundamental + news + macro, -35, 55)
    dividend_yield = dividend12m / price * 100 if price > 0 else 0.0
    target = price * (1 + price_return / 100)
    history_score = min(30.0, len(closes) / 8.5)
    trend_score = clamp(financial_trend, 0, 1) * 28
    news_score = min(18.0, abs(news) * 1.4)
    confidence = round(clamp(34 + history_score + trend_score + news_score, 35, 88))
    return {
        "targetPrice": round(target, 2),
        "priceReturn": round(price_return, 1),
        "dividendYield": round(dividend_yield, 1),
        "expectedReturn": round(price_return + dividend_yield, 1),
        "confidence": confidence,
        "targetModel": "tradesignal-v1",
        "targetDrivers": {
            "impulse": round(impulse, 1),
            "fundamental": round(fundamental, 1),
            "news": round(news, 1),
            "macro": round(macro, 1),
        },
        "return3m": round(ret_3m, 1),
        "return12m": round(ret_12m, 1),
    }


def synthetic_closes(price: float, return_3m: float, return_12m: float) -> list[float]:
    if price <= 0:
        return []
    start_12 = price / (1 + return_12m / 100) if return_12m > -95 else price
    start_3 = price / (1 + return_3m / 100) if return_3m > -95 else price
    closes = []
    for index in range(252):
        if index < 189:
            ratio = index / 189
            closes.append(start_12 * (1 - ratio) + start_3 * ratio)
        else:
            ratio = (index - 189) / 63
            closes.append(start_3 * (1 - ratio) + price * ratio)
    closes[-1] = price
    return closes


def build_stocks(
    config: dict[str, Any],
    urgent_signals: list[dict[str, Any]] | None = None,
    previous_stocks: list[dict[str, Any]] | None = None,
    limit: int | None = RANKING_LIMIT,
) -> list[dict[str, Any]]:
    forecasts = config["stocks"]
    previous_by_id = {item["secid"]: item for item in (previous_stocks or [])}
    try:
        quotes = {
            item["SECID"]: item
            for item in fetch_board("shares", "TQBR", [item["secid"] for item in forecasts])
        }
    except Exception:
        quotes = {}
    impacts = news_impact_map(urgent_signals or [])
    rate_drop = max(
        0.0,
        number(config["macro"]["currentKeyRate"]) - number(config["macro"]["forecastKeyRate12m"]),
    )
    histories: dict[str, list[float]] = {}
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(daily_closes, forecast["secid"], ("TQBR",)): forecast["secid"]
            for forecast in forecasts
        }
        for future in as_completed(futures):
            secid = futures[future]
            try:
                histories[secid] = future.result()
            except (RuntimeError, urllib.error.URLError, TimeoutError):
                histories[secid] = []

    output = []
    for forecast in forecasts:
        quote = quotes.get(forecast["secid"], {})
        previous = previous_by_id.get(forecast["secid"], {})
        price = market_price(quote, quote)
        if price <= 0:
            price = number(previous.get("price"))
        if price <= 0:
            continue
        closes = histories.get(forecast["secid"], [])
        if len(closes) < 45:
            closes = synthetic_closes(
                price,
                number(previous.get("return3m")),
                number(previous.get("return12m")),
            )
        override = number(forecast["targetPrice"]) if forecast.get("targetPrice") is not None else 0.0
        estimate = estimate_stock_target(
            price=price,
            closes=closes,
            financial_trend=number(forecast.get("financialTrend"), 0.5),
            dividend12m=number(forecast.get("dividend12m")),
            news_impact=impacts.get(forecast["secid"], 0.0),
            rate_drop=rate_drop,
            sector=str(forecast.get("sector") or ""),
            override_target=override or None,
        )
        output.append({
            "secid": forecast["secid"],
            "name": forecast["name"],
            "price": round(price, 2),
            "dayChange": round(number(quote.get("LASTTOPREVPRICE"), number(previous.get("dayChange"))), 2),
            "targetPrice": estimate["targetPrice"],
            "dividend12m": number(forecast.get("dividend12m")),
            "priceReturn": estimate["priceReturn"],
            "dividendYield": estimate["dividendYield"],
            "expectedReturn": estimate["expectedReturn"],
            "confidence": estimate["confidence"],
            "financialTrend": round(number(forecast.get("financialTrend")) * 100),
            "return3m": estimate.get("return3m", 0.0),
            "return12m": estimate.get("return12m", 0.0),
            "targetModel": estimate["targetModel"],
            "targetDrivers": estimate["targetDrivers"],
            "thesis": forecast["thesis"],
            "risks": forecast["risks"],
            "liquidityRub": round(
                number(quote.get("VALTODAY_RUR") or quote.get("VALTODAY") or previous.get("liquidityRub"))
            ),
            "sources": forecast.get("sources") or [{
                "publisher": "TradeSignal model",
                "publishedAt": date.today().isoformat(),
                "url": "https://iss.moex.com/iss/reference/",
            }],
        })
    if not output:
        raise RuntimeError("не удалось рассчитать ни одной акции")
    ranked = sorted(
        output,
        key=lambda item: (item["expectedReturn"], item["confidence"]),
        reverse=True,
    )
    if limit is None:
        return ranked
    return ranked[:limit]


def session_move(stock: dict[str, Any], previous: dict[str, Any] | None = None) -> float:
    """Last session / snapshot move, preferring the deeper drop."""
    quote_move = number(stock.get("dayChange"))
    moves = [quote_move]
    prev_price = number((previous or {}).get("price"))
    price = number(stock.get("price"))
    if prev_price > 0 and price > 0:
        moves.append((price / prev_price - 1) * 100)
    return round(min(moves), 2)


def scalp_news_for(ticker: str, urgent: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in urgent if str(item.get("ticker") or "") == ticker]


def is_fundamental_scalp_block(news: list[dict[str, Any]]) -> bool:
    for item in news:
        event = str(item.get("eventType") or "other")
        if event in FUNDAMENTAL_SCALP_BLOCK and item.get("action") == "SELL":
            return True
        if event == "trading_halt":
            return True
    return False


def scalp_catalyst(
    move: float,
    excess: float,
    market_median: float,
    news: list[dict[str, Any]],
) -> tuple[str, str]:
    legal = next(
        (item for item in news if item.get("eventType") in NONFUNDAMENTAL_EVENTS and item.get("action") == "SELL"),
        None,
    )
    if legal is not None:
        title = str(legal.get("title") or "нефундаментальная новость")
        return "nonfundamental_news", f"Нефундаментальный негатив: {title}"
    if market_median <= -0.8 and move <= market_median - 0.2:
        return "market_drawdown", "Бумага просела вместе с рынком, без явного фундаментального удара."
    if excess >= 1.5:
        return "relative_weakness", "Краткосрочная конъюнктура: просадка сильнее медианы рынка."
    if move <= -2.0:
        return "session_washout", "Резкая сессионная просадка без сильного корпоративного сигнала."
    return "session_washout", "Краткосрочная просадка без подтвержденного фундамента."


def scalp_score(move: float, excess: float, catalyst: str, liquidity: float) -> float:
    base = 38 + min(28.0, abs(min(move, 0)) * 6.5) + min(22.0, max(0.0, excess) * 7.0)
    if catalyst == "nonfundamental_news":
        base += 10
    elif catalyst == "relative_weakness":
        base += 6
    elif catalyst == "market_drawdown":
        base += 4
    if liquidity >= 50_000_000:
        base += 4
    elif liquidity < 3_000_000:
        base -= 8
    return round(clamp(base, 20, 96), 1)


def build_scalp_signals(
    stocks: list[dict[str, Any]],
    urgent: list[dict[str, Any]] | None = None,
    previous_stocks: list[dict[str, Any]] | None = None,
    limit: int = SCALP_LIMIT,
) -> list[dict[str, Any]]:
    if not stocks:
        return []
    previous_by_id = {item.get("secid"): item for item in (previous_stocks or [])}
    moves = [session_move(item, previous_by_id.get(item.get("secid"))) for item in stocks]
    market_median = round(statistics.median(moves), 2) if moves else 0.0
    candidates: list[dict[str, Any]] = []
    for stock, move in zip(stocks, moves):
        ticker = str(stock.get("secid") or "")
        if not ticker:
            continue
        news = scalp_news_for(ticker, urgent or [])
        if is_fundamental_scalp_block(news):
            continue
        liquidity = number(stock.get("liquidityRub"))
        if liquidity and liquidity < 2_000_000:
            continue
        excess = round(market_median - move, 2)
        has_legal = any(
            item.get("eventType") in NONFUNDAMENTAL_EVENTS and item.get("action") == "SELL"
            for item in news
        )
        wide_market = market_median <= -0.8 and move <= min(-1.2, market_median - 0.2)
        relative = excess >= 1.5 and move <= -1.2
        washout = move <= -2.4
        legal_drop = has_legal and move <= -1.5
        if not (wide_market or relative or washout or legal_drop):
            continue
        catalyst, summary = scalp_catalyst(move, excess, market_median, news)
        score = scalp_score(move, excess, catalyst, liquidity)
        entity = ENTITY_BY_SECID.get(ticker, {})
        name = str(stock.get("name") or entity.get("name") or ticker)
        candidates.append({
            "ticker": ticker,
            "name": name,
            "hashtags": list(dict.fromkeys([make_hashtag(name), f"#{ticker}"])),
            "action": "BUY",
            "horizon": "1–3 сессии",
            "price": number(stock.get("price")),
            "dayChange": move,
            "marketChange": market_median,
            "excessDrop": excess,
            "catalyst": catalyst,
            "strength": round(score),
            "signalScore": score,
            "title": f"{ticker} просела на {abs(move):.1f}%",
            "summary": summary,
            "source": {
                "publisher": "MOEX · сессия",
                "url": f"https://www.moex.com/ru/issue.aspx?code={urllib.parse.quote(ticker)}",
            },
        })
    return sorted(
        candidates,
        key=lambda item: (item["signalScore"], -item["dayChange"]),
        reverse=True,
    )[:limit]


def market_stance(day_change: float) -> str:
    if day_change >= 0.4:
        return "растет"
    if day_change <= -0.4:
        return "падает"
    return "боковик"


def long_candidate_tickers(
    stocks: list[dict[str, Any]],
    urgent: list[dict[str, Any]],
    limit: int = 3,
) -> list[dict[str, str]]:
    blocked = {
        str(item.get("ticker") or "")
        for item in urgent
        if item.get("action") == "SELL" and str(item.get("eventType") or "") in FUNDAMENTAL_SCALP_BLOCK
    }
    ranked = [
        item for item in stocks
        if item.get("secid") not in blocked
        and number(item.get("expectedReturn")) >= 6
        and number(item.get("confidence")) >= 48
    ]
    ranked.sort(key=lambda item: (number(item.get("expectedReturn")), number(item.get("confidence"))), reverse=True)
    return [
        {"secid": str(item["secid"]), "name": str(item.get("name") or item["secid"])}
        for item in ranked[:limit]
    ]


def build_market_brief(
    index: dict[str, Any],
    stocks: list[dict[str, Any]],
    urgent: list[dict[str, Any]] | None = None,
    macro: dict[str, Any] | None = None,
) -> dict[str, Any]:
    urgent = urgent or []
    macro = macro or {}
    day_change = number(index.get("dayChange"))
    stance = market_stance(day_change)
    moves = [number(item.get("dayChange")) for item in stocks]
    down_share = round(sum(1 for move in moves if move < 0) / len(moves), 2) if moves else 0.0
    median_move = round(statistics.median(moves), 2) if moves else 0.0
    severe = [
        item for item in urgent
        if item.get("action") == "SELL" and str(item.get("eventType") or "") in FUNDAMENTAL_SCALP_BLOCK
    ]
    legal = [
        item for item in urgent
        if item.get("action") == "SELL" and str(item.get("eventType") or "") in NONFUNDAMENTAL_EVENTS
    ]
    reasons: list[str] = []
    if down_share >= 0.65:
        reasons.append(f"спад широкий: {round(down_share * 100)}% бумаг вселенной в минусе")
    elif down_share <= 0.35 and moves:
        reasons.append(f"большинство бумаг в плюсе ({round((1 - down_share) * 100)}%)")
    else:
        reasons.append(f"медиана вселенной {median_move:+.1f}% при индексе {day_change:+.1f}%")
    if severe:
        title = str(severe[0].get("title") or severe[0].get("ticker") or "фундаментальный негатив")
        reasons.append(f"в ленте есть тяжелый негатив ({title})")
    elif legal:
        title = str(legal[0].get("title") or "нефундаментальный шум")
        reasons.append(f"есть шум без удара по бизнесу: {title}")
    rate = number(macro.get("currentKeyRate"))
    if rate >= 13:
        reasons.append(f"ключевая ставка {rate:.1f}% по-прежнему давит на оценку акций")
    why = " ".join(
        f"{part[0].upper()}{part[1:]}{'' if part.endswith('.') else '.'}"
        for part in reasons[:3]
        if part
    ) or "Дневное движение индекса пока без явного единого драйвера."

    fundamental_drag = bool(severe) or day_change <= -2.0
    if fundamental_drag:
        horizon = "не только сессия"
        outlook = (
            "Это уже не чистый внутридневной шум: негатив или глубокая просадка "
            "могут держаться дольше одной сессии. Долгосрочный тренд по одному дню не переписываем."
        )
    elif abs(day_change) < 1.2:
        horizon = "краткосрочно"
        outlook = (
            "Пока это сессионная конъюнктура, а не смена долгосрочной перспективы. "
            "Горизонт 12 месяцев смотрите в блоке лучших идей, не в дневном проценте IMOEX."
        )
    else:
        horizon = "краткосрочно"
        outlook = (
            "Импульс дня сильный, но для долгосрочного вывода его недостаточно. "
            "Не путайте суточный ход индекса с новым циклом рынка."
        )

    longs = long_candidate_tickers(stocks, urgent)
    names = ", ".join(item["secid"] for item in longs) if longs else "список 12‑мес. идей ниже"
    if stance == "падает" and fundamental_drag:
        long_verdict = "не разгонять"
        long_advice = (
            f"Новые длинные сегодня лучше не наращивать широко. "
            f"Если горизонт именно 12 месяцев, точечно смотреть {names} — и только без плеча."
        )
    elif stance == "падает":
        long_verdict = "точечно"
        long_advice = (
            f"Длинные можно набирать аккуратно на просадке, не в весь рынок: {names}. "
            f"Это не сигнал усреднять слабые бумаги и не идея для скальпа."
        )
    elif stance == "растет":
        long_verdict = "не догонять"
        long_advice = (
            f"Индекс уже в плюсе — не догонять дневной импульс. "
            f"Длинные держать в лучших бумагах модели: {names}."
        )
    else:
        long_verdict = "точечно"
        long_advice = (
            f"Рынок без явного дневного тренда. Длинные — только в отдельных бумагах, не в индексе целиком: {names}."
        )

    return {
        "index": str(index.get("index") or "IMOEX"),
        "indexName": str(index.get("indexName") or "Индекс МосБиржи"),
        "value": round(number(index.get("value")), 2),
        "dayChange": round(day_change, 2),
        "stance": stance,
        "horizon": horizon,
        "longVerdict": long_verdict,
        "why": why,
        "outlook": outlook,
        "longAdvice": long_advice,
        "longTickers": longs,
        "breadthDown": down_share,
        "source": index.get("source") or {
            "publisher": "Московская биржа",
            "url": "https://www.moex.com/ru/index/IMOEX",
        },
    }


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def bond_candidate(item: dict[str, Any], board: str) -> bool:
    maturity = parse_date(item.get("MATDATE"))
    if not maturity or maturity < date.today() + timedelta(days=90):
        return False
    ytm = number(item.get("YIELD") or item.get("EFFECTIVEYIELD"))
    price = market_price(item, item)
    if not (2 <= ytm <= 35 and 45 <= price <= 160):
        return False
    if board == "TQOB":
        return str(item.get("SECID", "")).startswith("SU")
    name = f"{item.get('SHORTNAME', '')} {item.get('SECNAME', '')}".lower()
    trusted = (
        "ржд", "роснефт", "газпром", "сбер", "вэб", "дом.рф", "мтс",
        "норник", "россети", "росатом", "алроса", "транснефт", "совкомбанк",
    )
    return any(issuer in name for issuer in trusted)


def build_bonds(config: dict[str, Any]) -> list[dict[str, Any]]:
    rate_drop = max(0.0, number(config["macro"]["currentKeyRate"]) - number(config["macro"]["forecastKeyRate12m"]))
    candidates: list[dict[str, Any]] = []
    board_rows: dict[str, list[dict[str, Any]]] = {}
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {
            executor.submit(fetch_board, "bonds", board): board
            for board in ("TQOB", "TQCB")
        }
        for future in as_completed(futures):
            board = futures[future]
            board_rows[board] = future.result()
    for board in ("TQOB", "TQCB"):
        for item in board_rows.get(board, []):
            if not bond_candidate(item, board):
                continue
            ytm = number(item.get("YIELD") or item.get("EFFECTIVEYIELD"))
            duration_years = number(item.get("DURATION")) / 365
            price_effect = min(15.0, duration_years * rate_drop * 0.75)
            expected = ytm + price_effect
            maturity = str(item.get("MATDATE", ""))[:10]
            candidates.append({
                "secid": item["SECID"],
                "name": item.get("SHORTNAME") or item["SECID"],
                "kind": "ОФЗ" if board == "TQOB" else "Корпоративная",
                "price": round(market_price(item, item), 2),
                "yield": round(ytm, 2),
                "coupon": round(number(item.get("COUPONPERCENT")), 2),
                "durationYears": round(duration_years, 1),
                "maturity": maturity,
                "expectedReturn": round(expected, 1),
                "confidence": 88 if board == "TQOB" else 69,
                "liquidityRub": round(number(item.get("VALTODAY_RUR") or item.get("VALTODAY"))),
                "thesis": (
                    f"Текущая доходность {ytm:.1f}% и сценарный эффект снижения ставок "
                    f"около {price_effect:.1f}%."
                ),
                "risks": (
                    "Рост ставок вызывает снижение цены; результат зависит от реинвестирования купонов."
                    if board == "TQOB"
                    else "Кредитный рейтинг автоматически не проверяется; есть процентный и кредитный риск."
                ),
                "source": "https://iss.moex.com/iss/reference/",
            })
    liquid = [
        item for item in candidates
        if item["liquidityRub"] >= (100_000 if item["kind"] == "ОФЗ" else 25_000)
    ]
    pool = liquid or candidates
    corp = sorted(
        (x for x in pool if x["kind"] != "ОФЗ"),
        key=lambda x: x["expectedReturn"],
        reverse=True,
    )[:5]
    ofz = sorted(
        (x for x in pool if x["kind"] == "ОФЗ"),
        key=lambda x: x["expectedReturn"],
        reverse=True,
    )[:5]
    selected = ofz + corp
    if len(selected) < RANKING_LIMIT:
        used = {item["secid"] for item in selected}
        selected.extend(
            item
            for item in sorted(pool, key=lambda x: x["expectedReturn"], reverse=True)
            if item["secid"] not in used
        )
    return sorted(
        selected,
        key=lambda item: item["expectedReturn"],
        reverse=True,
    )[:RANKING_LIMIT]


def daily_closes(secid: str, boards: tuple[str, ...] = ("TQBR", "TQTF")) -> list[float]:
    start = (date.today() - timedelta(days=430)).isoformat()
    combined: list[tuple[str, float]] = []
    # Always merge TQBR + TQTF when both are requested: after the June 2026 board
    # migration TQBR alone often has only ~1-2 months of history.
    for board in boards:
        url = (
            f"{MOEX}/engines/stock/markets/shares/boards/{board}/securities/"
            f"{urllib.parse.quote(secid)}/candles.json?"
            + urllib.parse.urlencode({
                "from": start,
                "interval": 24,
                "iss.meta": "off",
                "limit": 500,
            })
        )
        try:
            candles = [
                (str(item.get("begin")), number(item.get("close")))
                for item in rows(get_json(url), "candles")
                if number(item.get("close")) > 0
            ]
        except RuntimeError:
            continue
        if candles:
            combined.extend(candles)
    return [value for _, value in sorted(dict(combined).items())]


def annualized_volatility(closes: list[float]) -> float:
    returns = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    return statistics.pstdev(returns) * math.sqrt(252) * 100 if len(returns) > 20 else 0.0


def max_drawdown(closes: list[float]) -> float:
    peak = closes[0]
    worst = 0.0
    for value in closes:
        peak = max(peak, value)
        worst = min(worst, (value / peak - 1) * 100)
    return worst


FUND_CATEGORY = {
    "LQDT": "money", "SBMM": "money", "AKMM": "money", "TMON": "money",
    "BOND": "bonds", "OBLG": "bonds", "SBRB": "bonds", "BCSD": "bonds",
    "RCMB": "bonds", "RSHU": "bonds", "AKPP": "bonds", "AMRB": "bonds",
    "GOLD": "gold", "TGLD": "gold", "SBGD": "gold", "GOLDETF": "gold",
    "TMOS": "equity", "SBMX": "equity", "EQMX": "equity", "AKME": "equity",
    "DIVD": "equity", "TECH": "equity", "GROD": "equity",
}
FUND_CATEGORY_LABEL = {
    "money": "Денежный рынок",
    "bonds": "Облигации",
    "gold": "Золото",
    "equity": "Акции",
}
MIN_FUND_HISTORY = 21
MIN_FUND_LIQUIDITY = 3_000_000


def fund_category(secid: str) -> str:
    return FUND_CATEGORY.get(secid, "equity")


def estimate_fund_return(
    closes: list[float],
    category: str,
    key_rate: float,
    rate_drop: float,
) -> dict[str, Any]:
    ret_3m = (closes[-1] / closes[-min(63, len(closes))] - 1) * 100
    span_12 = min(252, len(closes))
    ret_12m = (closes[-1] / closes[-span_12] - 1) * 100
    vol = annualized_volatility(closes)
    drawdown = max_drawdown(closes)

    if category == "money":
        carry = max(6.0, key_rate - 1.2)
        # Money-market NAV drifts slowly; short windows understate carry, so lean on the rate.
        expected = 0.8 * carry + 0.2 * max(ret_12m, 0.0)
        thesis = (
            f"Фонд денежного рынка: сценарная доходность около ключевой ставки "
            f"({key_rate:.1f}% минус издержки)."
        )
    elif category == "bonds":
        carry = max(5.0, key_rate - 1.8 + rate_drop * 1.2)
        expected = 0.3 * clamp(ret_12m, -15, 25) + 0.15 * clamp(ret_3m, -10, 15) + 0.55 * carry
        expected -= 0.04 * vol + 0.03 * abs(drawdown)
        thesis = "Облигационный фонд: ставка/купонный carry + смягчение ДКП, с поправкой на волатильность."
    elif category == "gold":
        expected = 0.4 * clamp(ret_12m, -30, 45) + 0.35 * clamp(ret_3m, -25, 30) - 0.08 * vol - 0.05 * abs(drawdown)
        thesis = "Золотой фонд: импульс цены металла с штрафом за просадку и волатильность."
    else:
        expected = 0.4 * clamp(ret_12m, -35, 45) + 0.35 * clamp(ret_3m, -30, 35) - 0.08 * vol - 0.05 * abs(drawdown)
        thesis = "Акционный фонд: тренд 3/12 мес. с поправкой на волатильность и просадку."

    confidence = max(
        35,
        min(
            84,
            round(58 + min(len(closes), 252) / 12 - vol * 0.35 + (8 if category in {"money", "bonds"} else 0)),
        ),
    )
    return {
        "return3m": round(ret_3m, 1),
        "return12m": round(ret_12m, 1),
        "volatility": round(vol, 1),
        "maxDrawdown": round(drawdown, 1),
        "expectedReturn": round(clamp(expected, -20, 40), 1),
        "confidence": confidence,
        "thesis": thesis,
    }


def build_funds(
    config: dict[str, Any],
    previous_funds: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    # Since 22 June 2026 MOEX trades exchange funds on the unified TQBR board.
    preferred_ids = config["funds"]["preferred"]
    previous_by_id = {item["secid"]: item for item in (previous_funds or [])}
    try:
        board = [
            item for item in fetch_board("shares", "TQBR", preferred_ids)
            if item.get("INSTRID") == "IFTF"
            or item.get("SECID") in preferred_ids
        ]
    except Exception:
        board = []
    by_id = {item["SECID"]: item for item in board}
    for secid in preferred_ids:
        if secid in by_id:
            continue
        prev = previous_by_id.get(secid)
        if not prev:
            continue
        by_id[secid] = {
            "SECID": secid,
            "SHORTNAME": prev.get("name") or secid,
            "LAST": prev.get("price"),
            "PREVPRICE": prev.get("price"),
            "LASTTOPREVPRICE": prev.get("dayChange"),
            "VALTODAY_RUR": prev.get("liquidityRub"),
        }
    preferred = [secid for secid in preferred_ids if secid in by_id]
    most_liquid = sorted(
        by_id.values(),
        key=lambda item: number(item.get("VALTODAY_RUR") or item.get("VALTODAY")),
        reverse=True,
    )
    universe = (preferred + [item["SECID"] for item in most_liquid if item["SECID"] not in preferred])[:20]
    histories: dict[str, list[float]] = {}
    if board:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(daily_closes, secid, ("TQBR", "TQTF")): secid
                for secid in universe
            }
            for future in as_completed(futures):
                secid = futures[future]
                try:
                    histories[secid] = future.result()
                except (RuntimeError, urllib.error.URLError, TimeoutError):
                    continue

    key_rate = number(config["macro"]["currentKeyRate"])
    rate_drop = max(0.0, key_rate - number(config["macro"]["forecastKeyRate12m"]))
    scored: list[dict[str, Any]] = []
    for secid in universe:
        item = by_id[secid]
        prev = previous_by_id.get(secid, {})
        closes = histories.get(secid, [])
        category = fund_category(secid)
        price = market_price(item, item)
        if price <= 0:
            price = number(prev.get("price"))
        if len(closes) < MIN_FUND_HISTORY and prev:
            closes = synthetic_closes(
                price or number(prev.get("price")),
                number(prev.get("return3m")),
                number(prev.get("return12m")),
            )
        if len(closes) < MIN_FUND_HISTORY and category in {"money", "bonds"} and price > 0:
            # Board migration often leaves money/bond ETFs without long candles;
            # synthesize a mild rate-like drift so carry funds are not dropped.
            daily = (key_rate / 100) / 252
            closes = [price / ((1 + daily) ** (120 - index)) for index in range(120)]
            closes[-1] = price
        if len(closes) < MIN_FUND_HISTORY or price <= 0:
            continue
        liquidity = number(item.get("VALTODAY_RUR") or item.get("VALTODAY") or prev.get("liquidityRub"))
        if liquidity < MIN_FUND_LIQUIDITY and secid not in set(preferred_ids[:8]):
            continue
        estimate = estimate_fund_return(closes, category, key_rate, rate_drop)
        scored.append({
            "secid": secid,
            "name": item.get("SHORTNAME") or prev.get("name") or secid,
            "category": category,
            "categoryLabel": FUND_CATEGORY_LABEL[category],
            "price": round(price, 4),
            "dayChange": round(number(item.get("LASTTOPREVPRICE"), number(prev.get("dayChange"))), 2),
            "return3m": estimate["return3m"],
            "return12m": estimate["return12m"],
            "volatility": estimate["volatility"],
            "maxDrawdown": estimate["maxDrawdown"],
            "expectedReturn": estimate["expectedReturn"],
            "confidence": estimate["confidence"],
            "liquidityRub": round(liquidity),
            "thesis": estimate["thesis"],
            "risks": "Прошлая доходность и сценарный carry не гарантируют будущий результат.",
            "source": "https://iss.moex.com/iss/reference/",
        })

    # Diversify top-10: keep best names across money/bonds/gold/equity, not only losers.
    selected: list[dict[str, Any]] = []
    used: set[str] = set()
    for category in ("money", "bonds", "gold", "equity"):
        bucket = sorted(
            (item for item in scored if item["category"] == category),
            key=lambda item: (item["expectedReturn"], item["liquidityRub"]),
            reverse=True,
        )
        take = 3 if category in {"money", "bonds"} else 2
        for item in bucket[:take]:
            selected.append(item)
            used.add(item["secid"])
    leftovers = sorted(
        (item for item in scored if item["secid"] not in used),
        key=lambda item: (item["expectedReturn"], item["liquidityRub"]),
        reverse=True,
    )
    selected.extend(leftovers)
    if not selected:
        raise RuntimeError("не удалось рассчитать ни одного фонда")
    return sorted(
        selected,
        key=lambda item: (item["expectedReturn"], item["confidence"], item["liquidityRub"]),
        reverse=True,
    )[:RANKING_LIMIT]


def strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()


def rss_items(source: str, url: str) -> list[dict[str, str]]:
    root = ET.fromstring(request_bytes(url))
    output = []
    for item in root.findall(".//item")[:40]:
        published_at = (
            item.findtext("pubDate", "")
            or item.findtext("{http://purl.org/dc/elements/1.1/}date", "")
            or item.findtext("date", "")
        )
        output.append({
            "source": source,
            "title": strip_html(item.findtext("title", "")),
            "description": strip_html(item.findtext("description", "")),
            "url": item.findtext("link", ""),
            "publishedAt": published_at,
        })
    return output


class TelegramChannelParser(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.items: list[dict[str, str]] = []
        self.current: dict[str, Any] | None = None
        self.text_depth = 0

    def _flush(self) -> None:
        if not self.current:
            return
        text = re.sub(r"\s+", " ", "".join(self.current["text"])).strip()
        published_at = self.current.get("publishedAt", "")
        if text and published_at:
            title = text[:220] + ("…" if len(text) > 220 else "")
            self.items.append({
                "source": self.source,
                "title": title,
                "description": text,
                "url": self.current.get("url") or f"https://t.me/{self.current['post']}",
                "publishedAt": published_at,
            })
        self.current = None
        self.text_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        post = attributes.get("data-post")
        if post:
            self._flush()
            self.current = {"post": post, "text": []}
        if not self.current:
            return
        classes = attributes.get("class", "") or ""
        if tag == "div" and "tgme_widget_message_text" in classes:
            self.text_depth = 1
        elif self.text_depth:
            self.text_depth += 1
        if tag == "br" and self.text_depth:
            self.current["text"].append(" ")
        if tag == "time" and attributes.get("datetime"):
            self.current["publishedAt"] = attributes["datetime"]
        if tag == "a" and "tgme_widget_message_date" in classes and attributes.get("href"):
            self.current["url"] = attributes["href"]

    def handle_endtag(self, tag: str) -> None:
        if self.text_depth:
            self.text_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.current and self.text_depth:
            self.current["text"].append(data)

    def finish(self) -> list[dict[str, str]]:
        self._flush()
        return self.items


def telegram_items(source: str, url: str) -> list[dict[str, str]]:
    parser = TelegramChannelParser(source)
    parser.feed(request_bytes(url).decode("utf-8", errors="replace"))
    return parser.finish()


def moex_sitenews() -> list[dict[str, str]]:
    payload = get_json(f"{MOEX}/sitenews.json?iss.meta=off&start=0")
    return [
        {
            "source": "Московская биржа",
            "title": strip_html(str(item.get("title", ""))),
            "description": "",
            "url": f"https://www.moex.com/n{item.get('id')}",
            "publishedAt": str(item.get("published_at", "")),
        }
        for item in rows(payload, "sitenews")
    ]


def make_hashtag(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-zА-Яа-яЁё]", "", value)
    return f"#{cleaned}" if cleaned else ""


def alias_matches(alias: str, text: str) -> bool:
    suffix = ""
    if len(alias) >= 5 and re.search(r"[бвгджзклмнпрстфхцчшщ]$", alias, flags=re.IGNORECASE):
        suffix = r"(?:а|у|ом|е|ы|и)?"
    pattern = rf"(?<![0-9A-Za-zА-Яа-яЁё]){re.escape(alias)}{suffix}(?![0-9A-Za-zА-Яа-яЁё])"
    if not re.search(pattern, text, flags=re.IGNORECASE):
        return False
    if alias in AMBIGUOUS_ALIASES:
        context = (
            "акци", "компан", "эмитент", "дивид", "выруч", "прибыл", "отчет",
            "ритейл", "производ", "золот", "добыч", "групп",
        )
        return any(marker in text for marker in context)
    return True


def is_negative_actor_only(title: str, ticker: str) -> bool:
    aliases = [
        alias.lower()
        for alias, alias_ticker in ISSUER_TICKERS.items()
        if alias_ticker == ticker
    ]
    lowered = title.lower()
    actor_actions = r"(?:инициир\w*|намерен\w*|обрат\w*|подал\w*)"
    negative_events = r"(?:банкрот\w*|иск\w*)"
    return any(
        re.search(
            rf"{re.escape(alias)}.{{0,55}}{actor_actions}.{{0,55}}{negative_events}",
            lowered,
        )
        for alias in aliases
    )


def collect_entity_candidates(
    text: str,
    stocks: list[dict[str, Any]],
    bonds: list[dict[str, Any]],
    funds: list[dict[str, Any]] | None = None,
) -> list[tuple[int, float, str, list[str], str]]:
    """Return candidates as (alias_len, confidence, ticker, tags, matched_alias)."""
    upper = text.upper()
    lower = text.lower()
    known_tickers = {stock["secid"] for stock in stocks} | set(ISSUER_TICKERS.values())
    fund_tickers = {fund["secid"] for fund in (funds or [])}
    candidates: list[tuple[int, float, str, list[str], str]] = []

    isin = re.search(r"\bRU[A-Z0-9]{10}\b", upper)
    if isin:
        value = isin.group(0)
        candidates.append((len(value), 0.99, value, [f"#{value}"], value.lower()))

    tagged_tickers = re.findall(r"#([A-Z]{4,5})\b", upper)
    for ticker in tagged_tickers:
        if ticker in fund_tickers or ticker in NON_TICKER_TOKENS:
            continue
        if ticker in known_tickers or "🇷🇺" in text:
            company_name = ENTITY_BY_SECID.get(ticker, {}).get("name", ticker)
            confidence = 0.98 if ticker in known_tickers else 0.65
            tags = list(dict.fromkeys([make_hashtag(company_name), f"#{ticker}"]))
            candidates.append((len(ticker), confidence, ticker, tags, f"#{ticker.lower()}"))

    for stock in stocks:
        ticker_match = re.search(rf"\b{re.escape(stock['secid'])}\b", upper)
        name = stock["name"].split(",")[0]
        name_match = alias_matches(name.lower(), lower)
        if ticker_match or name_match:
            tags = [make_hashtag(name), f"#{stock['secid']}"]
            confidence = 0.99 if ticker_match else 0.95
            alias = stock["secid"] if ticker_match else name
            alias_len = len(alias)
            candidates.append(
                (alias_len, confidence, stock["secid"], list(dict.fromkeys(tag for tag in tags if tag)), alias.lower())
            )
    for bond in bonds:
        if bond["secid"] in upper or alias_matches(str(bond["name"]).lower(), lower):
            tags = [make_hashtag(str(bond["name"])), f"#{bond['secid']}"]
            confidence = 0.99 if bond["secid"] in upper else 0.9
            alias = bond["secid"] if bond["secid"] in upper else str(bond["name"])
            candidates.append(
                (len(alias), confidence, bond["secid"], list(dict.fromkeys(tag for tag in tags if tag)), alias.lower())
            )
    for issuer, ticker_id in ISSUER_TICKERS.items():
        if alias_matches(issuer, lower):
            entity = ENTITY_BY_SECID[ticker_id]
            candidates.append((
                len(issuer),
                0.9,
                ticker_id,
                [make_hashtag(entity["name"]), f"#{ticker_id}"],
                issuer,
            ))
    if re.search(r"акци|бумаг|облигац|тикер", lower):
        tickers = re.findall(r"\b[A-Z]{4,5}\b", upper)
        ticker = next((value for value in tickers if value not in NON_TICKER_TOKENS), None)
        if ticker and not any(item[2] == ticker for item in candidates):
            candidates.append((len(ticker), 0.6, ticker, [f"#{ticker}"], ticker.lower()))
    return candidates


def related_instrument(
    text: str,
    stocks: list[dict[str, Any]],
    bonds: list[dict[str, Any]],
    funds: list[dict[str, Any]] | None = None,
) -> tuple[str, list[str], float] | None:
    candidates = collect_entity_candidates(text, stocks, bonds, funds)
    if not candidates:
        return None
    # Prefer the longest alias so "Сбербанк ... Евротранс" resolves to EUTR, not SBER.
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    _, confidence, ticker, tags, _ = candidates[0]
    return ticker, tags, confidence


def event_marker_spans(text: str) -> list[tuple[int, str]]:
    spans: list[tuple[int, str]] = []
    for event_type, markers in EVENT_TAXONOMY.items():
        for marker in markers:
            start = 0
            while True:
                index = text.find(marker, start)
                if index < 0:
                    break
                spans.append((index, event_type))
                start = index + len(marker)
    return spans


def alias_positions(text: str, alias: str) -> list[int]:
    positions: list[int] = []
    if not alias:
        return positions
    start = 0
    needle = alias.lower()
    while True:
        index = text.find(needle, start)
        if index < 0:
            break
        positions.append(index)
        start = index + len(needle)
    return positions


def entity_event_proximity(text: str, alias: str, event_type: str) -> float:
    """1.0 = entity next to event marker; 0.2 = far / missing."""
    lower = text.lower()
    entity_positions = alias_positions(lower, alias.lower())
    if not entity_positions:
        return 0.35
    marker_positions = [
        index for index, marker_type in event_marker_spans(lower)
        if marker_type == event_type or event_type in WEAK_EVENT_TYPES
    ]
    if not marker_positions:
        marker_positions = [index for index, _ in event_marker_spans(lower)]
    if not marker_positions:
        return 0.55
    distance = min(abs(entity - marker) for entity in entity_positions for marker in marker_positions)
    if distance <= 24:
        return 1.0
    if distance <= 60:
        return 0.85
    if distance <= 120:
        return 0.65
    if distance <= 220:
        return 0.45
    return 0.25


def resolve_related_instrument(
    title: str,
    description: str,
    stocks: list[dict[str, Any]],
    bonds: list[dict[str, Any]],
    funds: list[dict[str, Any]] | None,
    event_type: str,
) -> tuple[str, list[str], float, float] | None:
    """Title-first linking with description fallback and proximity re-rank.

    Returns ticker, tags, confidence, proximity (0..1).
    """
    title_hit = related_instrument(title, stocks, bonds, funds)
    instrument = title_hit
    used_text = title
    if instrument is None or instrument[2] < 0.9:
        combined = f"{title} {description}".strip()
        combined_hit = related_instrument(combined, stocks, bonds, funds)
        if combined_hit is not None and (instrument is None or combined_hit[2] >= instrument[2]):
            instrument = combined_hit
            used_text = combined
    if instrument is None:
        return None

    candidates = collect_entity_candidates(used_text, stocks, bonds, funds)
    by_ticker: dict[str, tuple[int, float, str, list[str], str]] = {}
    for candidate in candidates:
        current = by_ticker.get(candidate[2])
        if current is None or candidate[0] > current[0] or (
            candidate[0] == current[0] and candidate[1] > current[1]
        ):
            by_ticker[candidate[2]] = candidate
    strong = [
        item for item in by_ticker.values()
        if item[1] >= 0.9 and not str(item[2]).startswith("RU")
    ]
    alias = next((item[4] for item in candidates if item[2] == instrument[0]), instrument[0])
    if len(strong) >= 2:
        ranked = sorted(
            (
                (
                    entity_event_proximity(used_text, item[4], event_type),
                    item[0],
                    item[1],
                    item[2],
                    item[3],
                    item[4],
                )
                for item in strong
            ),
            reverse=True,
        )
        best, second = ranked[0], ranked[1]
        if best[0] >= second[0] + 0.15:
            instrument = (best[3], best[4], best[2])
            alias = best[5]
        elif abs(best[1] - second[1]) <= 2 and best[0] < 0.75:
            # Two peers in one headline without a clear event anchor.
            return None

    proximity = entity_event_proximity(used_text, alias, event_type)
    if title_hit is not None and title_hit[0] == instrument[0]:
        proximity = min(1.0, proximity + 0.08)
    return instrument[0], instrument[1], instrument[2], proximity


def evaluate_entity_linking(
    stocks: list[dict[str, Any]],
    bonds: list[dict[str, Any]],
    funds: list[dict[str, Any]],
) -> dict[str, Any]:
    samples = json.loads(ENTITY_EVAL_PATH.read_text(encoding="utf-8"))
    true_positive = false_positive = false_negative = 0
    for sample in samples:
        result = related_instrument(sample["text"], stocks, bonds, funds)
        predicted = result[0] if result else None
        expected = sample["expected"]
        if predicted == expected and expected is not None:
            true_positive += 1
        elif predicted != expected:
            false_positive += int(predicted is not None)
            false_negative += int(expected is not None)
    precision_denominator = true_positive + false_positive
    recall_denominator = true_positive + false_negative
    return {
        "entityLinkPrecision": round(true_positive / precision_denominator, 3) if precision_denominator else 1.0,
        "entityLinkRecall": round(true_positive / recall_denominator, 3) if recall_denominator else 1.0,
        "entityEvalSamples": len(samples),
    }


def event_key(
    text: str,
    direction: str,
) -> str:
    for group, markers in EVENT_TAXONOMY.items():
        if any(marker in text for marker in markers):
            return group
    dictionary = NEGATIVE_WORDS if direction == "SELL" else POSITIVE_WORDS
    matches = [(weight, keyword) for keyword, weight in dictionary.items() if keyword in text]
    return max(matches)[1] if matches else direction.lower()


def is_mechanical_dividend_event(text: str) -> bool:
    return bool(
        re.search(r"дивидендн\w*(?:\s+\w+){0,2}\s+(?:гэп|отсеч)", text)
        and "закрыл" not in text
    )


def should_merge_signals(
    existing: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    if existing["ticker"] != candidate["ticker"] or existing["action"] != candidate["action"]:
        return False
    same_event = existing["_event"] == candidate["_event"]
    similar = jaccard_similarity(existing["_tokens"], candidate["_tokens"]) >= 0.52
    weak_event = existing["_event"] in WEAK_EVENT_TYPES or candidate["_event"] in WEAK_EVENT_TYPES
    within_window = abs((existing["_published"] - candidate["_published"]).total_seconds()) <= 129_600
    if similar and same_event:
        return True
    if similar and not weak_event:
        # High textual overlap but taxonomy disagreed — still merge only if both non-weak? Prefer not.
        return False
    if same_event and within_window and not weak_event:
        return True
    return False


def novelty_score(source_count: int) -> float:
    # First print = fully novel; extra sources are confirmation, not a new catalyst.
    return round(max(0.35, 1.0 - 0.22 * max(0, source_count - 1)), 3)


def source_quality_score(priority: int, source_count: int) -> float:
    base = priority / 4
    confirmation = min(0.18, 0.08 * max(0, source_count - 1))
    return round(min(1.0, base + confirmation), 3)


def market_reaction_for(
    ticker: str,
    action: str,
    day_changes: dict[str, float],
) -> dict[str, Any] | None:
    if ticker not in day_changes:
        return None
    day_change = day_changes[ticker]
    confirmed = (action == "BUY" and day_change >= 1.5) or (action == "SELL" and day_change <= -1.5)
    divergence = (action == "BUY" and day_change <= -1.5) or (action == "SELL" and day_change >= 1.5)
    return {
        "dayChangePct": round(day_change, 2),
        "confirmed": confirmed,
        "divergence": divergence,
    }


def compute_signal_score(signal: dict[str, Any]) -> float:
    severity = float(signal.get("eventSeverity", EVENT_SEVERITY.get(signal.get("eventType", "other"), 20)))
    entity = float(signal.get("entityConfidence", 50))
    impact_conf = float(signal.get("impactConfidence", 50))
    source_q = float(signal.get("sourceQuality", 0.25)) * 100
    novelty = float(signal.get("noveltyScore", 1.0)) * 100
    proximity = float(signal.get("_proximity", 0.55))
    strength = float(signal.get("strength", 50))
    market = signal.get("marketReaction") or {}
    market_term = 50.0
    if market:
        if market.get("confirmed"):
            market_term = 72.0
        elif market.get("divergence"):
            market_term = 58.0
        else:
            market_term = 50.0 + max(-8.0, min(8.0, float(market.get("dayChangePct", 0)) * 0.4))
    score = (
        0.30 * severity
        + 0.20 * entity
        + 0.15 * impact_conf
        + 0.10 * source_q
        + 0.10 * novelty
        + 0.10 * market_term
        + 0.05 * strength
    )
    return round(score * (0.75 + 0.25 * proximity), 2)


STOP_WORDS = {
    "для", "или", "это", "как", "что", "при", "после", "своих", "будет", "были",
    "его", "она", "они", "the", "and", "for", "with", "from", "today", "company",
}


def text_shingles(value: str) -> set[str]:
    tokens = [
        token
        for token in re.findall(r"[0-9A-Za-zА-Яа-яЁё]{3,}", value.lower())
        if token not in STOP_WORDS
    ]
    if len(tokens) < 2:
        return set(tokens)
    return {f"{left}:{right}" for left, right in zip(tokens, tokens[1:])}


def jaccard_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def impact_estimate(
    direction: str,
    event_strength: int,
    entity_confidence: float,
    source_priority: int,
) -> tuple[float, float, int]:
    sign = -1 if direction == "SELL" else 1
    sentiment = round(sign * min(1.0, event_strength / 45), 2)
    impact = round(sign * min(8.0, (event_strength * 0.08 + source_priority * 0.15) * entity_confidence), 1)
    confidence = round(min(92, 30 + event_strength * 0.8 + entity_confidence * 25 + source_priority * 3))
    return sentiment, impact, confidence


def published_datetime(value: str) -> datetime:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MOSCOW_TZ)
    return parsed.astimezone(MOSCOW_TZ)


def build_urgent(
    stocks: list[dict[str, Any]],
    bonds: list[dict[str, Any]],
    funds: list[dict[str, Any]],
    day_changes: dict[str, float] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, Any]]:
    started_at = time.perf_counter()
    day_changes = day_changes or {}
    health = []
    news: list[dict[str, str]] = []
    try:
        items = moex_sitenews()
        news.extend(items)
        health.append({"source": "Московская биржа", "status": "ok", "detail": f"{len(items)} сообщений"})
    except Exception as exc:
        health.append({"source": "Московская биржа", "status": "error", "detail": str(exc)[:140]})
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(rss_items, source, url): source
            for source, url in RSS_FEEDS
        }
        futures.update({
            executor.submit(telegram_items, source, url): source
            for source, url in TELEGRAM_FEEDS
        })
        for future in as_completed(futures):
            source = futures[future]
            try:
                items = future.result()
                news.extend(items)
                health.append({"source": source, "status": "ok", "detail": f"{len(items)} сообщений"})
            except Exception as exc:  # one feed failure must not block the snapshot
                health.append({"source": source, "status": "error", "detail": str(exc)[:140]})

    metrics = {
        "fetched": len(news),
        "expired": 0,
        "lowImpact": 0,
        "unlinked": 0,
        "duplicatesMerged": 0,
    }
    signal_clusters: list[dict[str, Any]] = []
    for item in news:
        try:
            published = published_datetime(item["publishedAt"])
            if datetime.now(MOSCOW_TZ) - published > timedelta(hours=36):
                metrics["expired"] += 1
                continue
        except (TypeError, ValueError, OverflowError):
            metrics["expired"] += 1
            continue
        text = f"{item['title']} {item['description']}".lower()
        positive = sum(weight for keyword, weight in POSITIVE_WORDS.items() if keyword in text)
        negative = sum(weight for keyword, weight in NEGATIVE_WORDS.items() if keyword in text)
        strength = max(positive, negative)
        if strength < 14:
            metrics["lowImpact"] += 1
            continue
        if is_mechanical_dividend_event(text):
            metrics["lowImpact"] += 1
            continue
        direction = "SELL" if negative > positive else "BUY"
        event = event_key(text, direction)
        event_type = event if event in EVENT_SEVERITY else "other"
        try:
            resolved = resolve_related_instrument(
                item["title"],
                item.get("description", ""),
                stocks,
                bonds,
                funds,
                event_type,
            )
        except Exception:
            # Enhancement must never kill the detector: fall back to title-only link.
            base = related_instrument(item["title"], stocks, bonds, funds)
            resolved = (*base, 0.55) if base else None
        if resolved is None:
            metrics["unlinked"] += 1
            continue
        ticker, hashtags, entity_confidence, proximity = resolved
        if direction == "SELL" and is_negative_actor_only(item["title"], ticker):
            metrics["unlinked"] += 1
            continue
        source = {"publisher": item["source"], "url": item["url"]}
        priority = SOURCE_PRIORITY.get(item["source"], 1)
        sentiment, impact, impact_confidence = impact_estimate(
            direction,
            strength,
            entity_confidence,
            priority,
        )
        severity = EVENT_SEVERITY.get(event_type, EVENT_SEVERITY["other"])
        market_reaction = market_reaction_for(ticker, direction, day_changes)
        candidate = {
            "ticker": ticker,
            "hashtags": hashtags,
            "action": direction,
            "strength": min(99, 48 + strength),
            "sentimentScore": sentiment,
            "impactEstimatePct": impact,
            "impactConfidence": impact_confidence,
            "entityConfidence": round(entity_confidence * 100),
            "impactModel": "rules-v1",
            "eventType": event_type,
            "eventSeverity": severity,
            "noveltyScore": 1.0,
            "sourceQuality": source_quality_score(priority, 1),
            "marketReaction": market_reaction,
            "title": item["title"],
            "summary": (
                "Негативное событие: сначала проверьте позицию и первоисточник."
                if direction == "SELL"
                else "Позитивный катализатор: дождитесь подтверждения рынком и проверьте первоисточник."
            ),
            "publishedAt": item["publishedAt"],
            "expiresAt": (datetime.now(MOSCOW_TZ) + timedelta(hours=24 if strength >= 30 else 48)).isoformat(),
            "source": source,
            "sources": [source],
            "sourceCount": 1,
            "_priority": priority,
            "_event": event_type,
            "_published": published,
            "_tokens": text_shingles(item["title"]),
            "_proximity": proximity,
        }
        candidate["signalScore"] = compute_signal_score(candidate)
        existing = next(
            (signal for signal in signal_clusters if should_merge_signals(signal, candidate)),
            None,
        )
        if existing is None:
            signal_clusters.append(candidate)
            continue
        metrics["duplicatesMerged"] += 1
        if source["url"] and source["url"] not in {value["url"] for value in existing["sources"]}:
            existing["sources"].append(source)
            existing["sourceCount"] = len(existing["sources"])
        existing["strength"] = max(existing["strength"], candidate["strength"])
        existing["impactConfidence"] = min(97, max(existing["impactConfidence"], candidate["impactConfidence"]) + 4)
        existing["eventSeverity"] = max(existing.get("eventSeverity", 0), candidate["eventSeverity"])
        existing["_proximity"] = max(existing.get("_proximity", 0), candidate["_proximity"])
        existing["noveltyScore"] = novelty_score(existing["sourceCount"])
        existing["sourceQuality"] = source_quality_score(
            max(existing["_priority"], candidate["_priority"]),
            existing["sourceCount"],
        )
        if candidate.get("marketReaction") and (
            existing.get("marketReaction") is None
            or abs(candidate["marketReaction"]["dayChangePct"]) >= abs(existing["marketReaction"]["dayChangePct"])
        ):
            existing["marketReaction"] = candidate["marketReaction"]
        if (
            candidate["_priority"] > existing["_priority"]
            or (
                candidate["_priority"] == existing["_priority"]
                and candidate["_published"] > existing["_published"]
            )
        ):
            for field in (
                "title", "summary", "publishedAt", "source", "_priority",
                "_published", "sentimentScore", "impactEstimatePct", "entityConfidence",
                "eventType", "eventSeverity",
            ):
                existing[field] = candidate[field]
        existing["signalScore"] = compute_signal_score(existing)

    signals = signal_clusters
    for signal in signals:
        signal["noveltyScore"] = novelty_score(signal.get("sourceCount", 1))
        signal["sourceQuality"] = source_quality_score(signal.get("_priority", 1), signal.get("sourceCount", 1))
        signal["signalScore"] = compute_signal_score(signal)
        for internal_field in ("_priority", "_event", "_published", "_tokens", "_proximity"):
            signal.pop(internal_field, None)
    matched_documents = len(signals) + metrics["duplicatesMerged"]
    metrics.update({
        "signals": len(signals),
        "dedupRate": round(metrics["duplicatesMerged"] / matched_documents, 3) if matched_documents else 0.0,
        "impactAccuracy": None,
        "latencyMs": round((time.perf_counter() - started_at) * 1000),
    })
    # Held-out eval uses a fixed instrument set, not the live universe order.
    metrics.update(evaluate_entity_linking([], [], [{"secid": "LQDT"}, {"secid": "BOND"}]))
    return sorted(
        signals,
        key=lambda item: (item.get("signalScore", item["strength"]), item["publishedAt"]),
        reverse=True,
    )[:URGENT_LIMIT], health, metrics


def previous_day_changes(previous: dict[str, Any]) -> dict[str, float]:
    changes: dict[str, float] = {}
    for section in ("stocks", "bonds", "funds"):
        for item in previous.get(section, []):
            ticker = item.get("secid") or item.get("ticker")
            value = item.get("dayChange")
            if ticker and isinstance(value, (int, float)):
                changes[str(ticker)] = float(value)
    return changes


def previous_data() -> dict[str, Any]:
    try:
        return json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main() -> int:
    started_at = time.perf_counter()
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    previous = previous_data()
    status: list[dict[str, str]] = []

    def safe_build(name: str, builder, fallback_key: str):
        section_started = time.perf_counter()
        try:
            value = builder()
            if not value:
                raise RuntimeError("источник вернул пустой набор")
            elapsed_ms = round((time.perf_counter() - section_started) * 1000)
            status.append({
                "source": name,
                "status": "ok",
                "detail": f"{len(value)} инструментов · {elapsed_ms} мс",
            })
            print(f"{name}: {len(value)} · {elapsed_ms} мс", flush=True)
            return value
        except Exception as exc:
            elapsed_ms = round((time.perf_counter() - section_started) * 1000)
            fallback = previous.get(fallback_key, [])
            status.append({
                "source": name,
                "status": "stale" if fallback else "error",
                "detail": f"{str(exc)[:140]}; сохранен прошлый снимок" if fallback else str(exc)[:140],
            })
            print(f"{name}: ошибка за {elapsed_ms} мс · {exc}", flush=True)
            return fallback

    bonds = safe_build("MOEX: облигации", lambda: build_bonds(config), "bonds")
    funds = safe_build(
        "MOEX: фонды",
        lambda: build_funds(config, previous.get("funds", [])),
        "funds",
    )
    stock_stubs = [
        {"secid": item["secid"], "name": item["name"]}
        for item in config.get("stocks", [])
    ]
    urgent_started = time.perf_counter()
    urgent, feed_health, pipeline_metrics = build_urgent(
        stock_stubs,
        bonds,
        funds,
        previous_day_changes(previous),
    )
    print(
        f"Срочные сигналы: {len(urgent)} · "
        f"{round((time.perf_counter() - urgent_started) * 1000)} мс",
        flush=True,
    )
    if not urgent:
        urgent = [
            item for item in previous.get("urgent", [])
            if item.get("expiresAt", "") > datetime.now(MOSCOW_TZ).isoformat()
            and item.get("ticker") not in {"РЫНОК", "НЕФТЕГАЗ"}
            and item.get("hashtags")
        ]
    stocks_universe = safe_build(
        "MOEX: акции",
        lambda: build_stocks(config, urgent, previous.get("stocks", []), limit=None),
        "stocks",
    )
    stocks = stocks_universe[:RANKING_LIMIT]
    scalp = build_scalp_signals(stocks_universe, urgent, previous.get("stocks", []))
    print(f"Скальп-отскок: {len(scalp)}", flush=True)
    if not scalp:
        scalp = [
            item for item in previous.get("scalp", [])
            if item.get("ticker") and item.get("action") == "BUY"
        ][:SCALP_LIMIT]
    try:
        imoex = fetch_imoex()
        status.append({
            "source": "MOEX: IMOEX",
            "status": "ok",
            "detail": f"{imoex['value']:.0f} · {imoex['dayChange']:+.2f}%",
        })
        print(f"IMOEX: {imoex['value']} · {imoex['dayChange']:+.2f}%", flush=True)
    except Exception as exc:
        fallback_index = (previous.get("marketBrief") or {})
        imoex = {
            "index": "IMOEX",
            "indexName": fallback_index.get("indexName") or "Индекс МосБиржи",
            "value": number(fallback_index.get("value")),
            "dayChange": number(
                fallback_index.get("dayChange"),
                statistics.median([number(item.get("dayChange")) for item in stocks_universe]) if stocks_universe else 0.0,
            ),
            "source": (fallback_index.get("source") or {
                "publisher": "Московская биржа",
                "url": "https://www.moex.com/ru/index/IMOEX",
            }),
        }
        status.append({
            "source": "MOEX: IMOEX",
            "status": "stale" if fallback_index.get("value") else "error",
            "detail": str(exc)[:140],
        })
        print(f"IMOEX: ошибка · {exc}", flush=True)
    market_brief = build_market_brief(imoex, stocks_universe, urgent, config.get("macro", {}))

    payload = {
        "generatedAt": now_iso(),
        "methodologyVersion": config["methodologyVersion"],
        "market": "Московская биржа",
        "marketBrief": market_brief,
        "urgent": urgent,
        "scalp": scalp,
        "stocks": stocks,
        "bonds": bonds,
        "funds": funds,
        "macro": config["macro"],
        "stockModel": STOCK_MODEL,
        "fundModel": config["funds"]["model"],
        "pipelineMetrics": pipeline_metrics,
        "sourceHealth": status + feed_health,
        "disclaimer": "Информация не является индивидуальной инвестиционной рекомендацией. Прогнозы и дивиденды не гарантированы.",
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    total_ms = round((time.perf_counter() - started_at) * 1000)
    print(f"Снимок записан: {OUTPUT_PATH} ({payload['generatedAt']}) · всего {total_ms} мс")
    failed = [item for item in status if item["status"] == "error"]
    return 1 if failed and not (stocks or bonds or funds) else 0


if __name__ == "__main__":
    sys.exit(main())
