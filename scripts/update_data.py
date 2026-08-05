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
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "forecasts.json"
OUTPUT_PATH = ROOT / "data" / "market-data.json"
USER_AGENT = "TradeSignal/1.0 (+https://github.com/VasyaForester/TradeSignal)"
MOSCOW_TZ = timezone(timedelta(hours=3))

MOEX = "https://iss.moex.com/iss"
RSS_FEEDS = [("Банк России", "https://www.cbr.ru/rss/RssPress")]

POSITIVE_WORDS = {
    "дивиденд": 18, "рекомендовал выплат": 28, "выкуп": 24, "байбэк": 24,
    "сильнее ожиданий": 26, "рекордн": 16, "повысил прогноз": 25,
    "снизить ключевую ставку": 20, "возобнов": 14,
}
NEGATIVE_WORDS = {
    "дефолт": 45, "банкрот": 42, "арест": 38, "обыск": 35,
    "приостанов": 25, "отказ от дивиденд": 30, "убыток": 18,
    "слабее ожиданий": 24, "понизил прогноз": 25, "дискретный аукцион": 22,
    "нарушен": 18, "санкци": 20, "авар": 24,
}


def now_iso() -> str:
    return datetime.now(MOSCOW_TZ).replace(microsecond=0).isoformat()


def request_bytes(url: str, attempts: int = 2) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=12) as response:
                return response.read()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
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
    query = {"iss.meta": "off", "iss.only": "securities,marketdata"}
    if securities:
        query["securities"] = ",".join(securities)
    url = (
        f"{MOEX}/engines/stock/markets/{market}/boards/{board}/securities.json?"
        + urllib.parse.urlencode(query)
    )
    payload = get_json(url)
    static = {item["SECID"]: item for item in rows(payload, "securities")}
    dynamic = {item["SECID"]: item for item in rows(payload, "marketdata")}
    return [{**item, **dynamic.get(secid, {})} for secid, item in static.items()]


def freshness_confidence(sources: list[dict[str, Any]], financial_trend: float) -> int:
    newest = max(date.fromisoformat(item["publishedAt"]) for item in sources)
    age_days = max(0, (date.today() - newest).days)
    freshness = max(25.0, 100.0 - age_days * 0.16)
    corroboration = min(100.0, 58.0 + len(sources) * 18.0)
    return round(0.55 * freshness + 0.25 * corroboration + 20.0 * financial_trend)


def build_stocks(config: dict[str, Any]) -> list[dict[str, Any]]:
    forecasts = config["stocks"]
    quotes = {
        item["SECID"]: item
        for item in fetch_board("shares", "TQBR", [item["secid"] for item in forecasts])
    }
    output = []
    for forecast in forecasts:
        quote = quotes.get(forecast["secid"], {})
        price = market_price(quote, quote)
        if price <= 0:
            continue
        target = number(forecast["targetPrice"])
        dividend = number(forecast.get("dividend12m"))
        price_return = (target / price - 1) * 100
        dividend_yield = dividend / price * 100
        total_return = price_return + dividend_yield
        confidence = freshness_confidence(
            forecast["sources"], number(forecast.get("financialTrend"), 0.5)
        )
        output.append({
            "secid": forecast["secid"],
            "name": forecast["name"],
            "price": round(price, 2),
            "dayChange": round(number(quote.get("LASTTOPREVPRICE")), 2),
            "targetPrice": target,
            "dividend12m": dividend,
            "priceReturn": round(price_return, 1),
            "dividendYield": round(dividend_yield, 1),
            "expectedReturn": round(total_return, 1),
            "confidence": confidence,
            "financialTrend": round(number(forecast.get("financialTrend")) * 100),
            "thesis": forecast["thesis"],
            "risks": forecast["risks"],
            "liquidityRub": round(number(quote.get("VALTODAY_RUR") or quote.get("VALTODAY"))),
            "sources": forecast["sources"],
        })
    return sorted(output, key=lambda item: (item["expectedReturn"], item["confidence"]), reverse=True)[:5]


def parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def bond_candidate(item: dict[str, Any], board: str) -> bool:
    maturity = parse_date(item.get("MATDATE"))
    if not maturity or maturity < date.today() + timedelta(days=365):
        return False
    ytm = number(item.get("YIELD") or item.get("EFFECTIVEYIELD"))
    price = market_price(item, item)
    if not (2 <= ytm <= 35 and 45 <= price <= 160):
        return False
    if board == "TQOB":
        return str(item.get("SECID", "")).startswith("SU")
    name = f"{item.get('SHORTNAME', '')} {item.get('SECNAME', '')}".lower()
    trusted = ("ржд", "роснефт", "газпром", "сбер", "вэб", "дом.рф", "мтс", "норник")
    return any(issuer in name for issuer in trusted)


def build_bonds(config: dict[str, Any]) -> list[dict[str, Any]]:
    rate_drop = max(0.0, number(config["macro"]["currentKeyRate"]) - number(config["macro"]["forecastKeyRate12m"]))
    candidates: list[dict[str, Any]] = []
    for board in ("TQOB", "TQCB"):
        for item in fetch_board("bonds", board):
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
    liquid = [item for item in candidates if item["liquidityRub"] >= 100_000]
    pool = liquid or candidates
    corp = sorted((x for x in pool if x["kind"] != "ОФЗ"), key=lambda x: x["expectedReturn"], reverse=True)[:2]
    ofz = sorted(
        (x for x in pool if x["kind"] == "ОФЗ"),
        key=lambda x: x["expectedReturn"],
        reverse=True,
    )[: 5 - len(corp)]
    return sorted(ofz + corp, key=lambda item: item["expectedReturn"], reverse=True)[:5]


def daily_closes(secid: str) -> list[float]:
    start = (date.today() - timedelta(days=430)).isoformat()
    combined: list[tuple[str, float]] = []
    for board in ("TQTF", "TQBR"):
        url = (
            f"{MOEX}/engines/stock/markets/shares/boards/{board}/securities/"
            f"{urllib.parse.quote(secid)}/candles.json?"
            + urllib.parse.urlencode({"from": start, "interval": 24, "iss.meta": "off"})
        )
        try:
            combined.extend(
                (str(item.get("begin")), number(item.get("close")))
                for item in rows(get_json(url), "candles")
                if number(item.get("close")) > 0
            )
        except RuntimeError:
            continue
    # Funds migrated from TQTF to TQBR in June 2026; deduplicate the boundary by date.
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


def build_funds(config: dict[str, Any]) -> list[dict[str, Any]]:
    # Since 22 June 2026 MOEX trades exchange funds on the unified TQBR board.
    preferred_ids = config["funds"]["preferred"]
    board = [
        item for item in fetch_board("shares", "TQBR", preferred_ids)
        if item.get("INSTRID") == "IFTF"
        or item.get("SECID") in preferred_ids
    ]
    by_id = {item["SECID"]: item for item in board}
    preferred = [secid for secid in preferred_ids if secid in by_id]
    most_liquid = sorted(
        board, key=lambda item: number(item.get("VALTODAY_RUR") or item.get("VALTODAY")), reverse=True
    )
    universe = (preferred + [item["SECID"] for item in most_liquid if item["SECID"] not in preferred])[:18]
    histories: dict[str, list[float]] = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {executor.submit(daily_closes, secid): secid for secid in universe}
        for future in as_completed(futures):
            secid = futures[future]
            try:
                histories[secid] = future.result()
            except (RuntimeError, urllib.error.URLError, TimeoutError):
                continue

    result = []
    for secid in universe:
        item = by_id[secid]
        closes = histories.get(secid, [])
        if len(closes) < 45:
            continue
        ret_3m = (closes[-1] / closes[-min(63, len(closes))] - 1) * 100
        ret_12m = (closes[-1] / closes[-min(252, len(closes))] - 1) * 100
        vol = annualized_volatility(closes)
        drawdown = max_drawdown(closes)
        raw_forecast = 0.45 * ret_12m + 0.35 * ret_3m - 0.12 * vol - 0.08 * abs(drawdown)
        expected = max(-20.0, min(40.0, raw_forecast))
        confidence = max(35, min(82, round(72 - vol * 0.45 + min(len(closes), 252) / 30)))
        result.append({
            "secid": secid,
            "name": item.get("SHORTNAME") or secid,
            "price": round(market_price(item, item), 4),
            "dayChange": round(number(item.get("LASTTOPREVPRICE")), 2),
            "return3m": round(ret_3m, 1),
            "return12m": round(ret_12m, 1),
            "volatility": round(vol, 1),
            "maxDrawdown": round(drawdown, 1),
            "expectedReturn": round(expected, 1),
            "confidence": confidence,
            "liquidityRub": round(number(item.get("VALTODAY_RUR") or item.get("VALTODAY"))),
            "thesis": "Количественный тренд с поправкой на волатильность и максимальную просадку.",
            "risks": "Это экстраполяция рыночного тренда, а не консенсус аналитиков.",
            "source": "https://iss.moex.com/iss/reference/",
        })
    return sorted(result, key=lambda item: (item["expectedReturn"], item["confidence"]), reverse=True)[:5]


def strip_html(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value or "")).strip()


def rss_items(source: str, url: str) -> list[dict[str, str]]:
    root = ET.fromstring(request_bytes(url))
    output = []
    for item in root.findall(".//item")[:40]:
        output.append({
            "source": source,
            "title": strip_html(item.findtext("title", "")),
            "description": strip_html(item.findtext("description", "")),
            "url": item.findtext("link", ""),
            "publishedAt": item.findtext("pubDate", ""),
        })
    return output


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


def related_ticker(text: str, stocks: list[dict[str, Any]]) -> str:
    upper = text.upper()
    for stock in stocks:
        if stock["secid"] in upper or stock["name"].upper().split(",")[0] in upper:
            return stock["secid"]
    ticker = re.search(r"\b[A-Z]{4,5}\b", upper)
    if ticker:
        return ticker.group(0)
    return "РЫНОК"


def published_datetime(value: str) -> datetime:
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=MOSCOW_TZ)
    return parsed.astimezone(MOSCOW_TZ)


def build_urgent(stocks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    health = []
    news: list[dict[str, str]] = []
    try:
        items = moex_sitenews()
        news.extend(items)
        health.append({"source": "Московская биржа", "status": "ok", "detail": f"{len(items)} сообщений"})
    except Exception as exc:
        health.append({"source": "Московская биржа", "status": "error", "detail": str(exc)[:140]})
    for source, url in RSS_FEEDS:
        try:
            items = rss_items(source, url)
            news.extend(items)
            health.append({"source": source, "status": "ok", "detail": f"{len(items)} сообщений"})
        except Exception as exc:  # feed failure must not block the entire snapshot
            health.append({"source": source, "status": "error", "detail": str(exc)[:140]})

    signals = []
    seen = set()
    for item in news:
        try:
            published = published_datetime(item["publishedAt"])
            if datetime.now(MOSCOW_TZ) - published > timedelta(days=4):
                continue
        except (TypeError, ValueError, OverflowError):
            continue
        text = f"{item['title']} {item['description']}".lower()
        positive = sum(weight for keyword, weight in POSITIVE_WORDS.items() if keyword in text)
        negative = sum(weight for keyword, weight in NEGATIVE_WORDS.items() if keyword in text)
        strength = max(positive, negative)
        if strength < 18:
            continue
        ticker = related_ticker(text, stocks)
        key = (ticker, item["title"][:60])
        if key in seen:
            continue
        seen.add(key)
        direction = "SELL" if negative > positive else "BUY"
        signals.append({
            "ticker": ticker,
            "action": direction,
            "strength": min(99, 48 + strength),
            "title": item["title"],
            "summary": (
                "Негативное событие: сначала проверьте позицию и первоисточник."
                if direction == "SELL"
                else "Позитивный катализатор: дождитесь подтверждения рынком и проверьте первоисточник."
            ),
            "publishedAt": item["publishedAt"],
            "expiresAt": (datetime.now(MOSCOW_TZ) + timedelta(hours=36 if strength >= 30 else 72)).isoformat(),
            "source": {"publisher": item["source"], "url": item["url"]},
        })
    return sorted(signals, key=lambda item: item["strength"], reverse=True)[:5], health


def previous_data() -> dict[str, Any]:
    try:
        return json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def main() -> int:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    previous = previous_data()
    status: list[dict[str, str]] = []

    def safe_build(name: str, builder, fallback_key: str):
        try:
            value = builder()
            if not value:
                raise RuntimeError("источник вернул пустой набор")
            status.append({"source": name, "status": "ok", "detail": f"{len(value)} инструментов"})
            return value
        except Exception as exc:
            fallback = previous.get(fallback_key, [])
            status.append({
                "source": name,
                "status": "stale" if fallback else "error",
                "detail": f"{str(exc)[:140]}; сохранен прошлый снимок" if fallback else str(exc)[:140],
            })
            return fallback

    stocks = safe_build("MOEX: акции", lambda: build_stocks(config), "stocks")
    bonds = safe_build("MOEX: облигации", lambda: build_bonds(config), "bonds")
    funds = safe_build("MOEX: фонды", lambda: build_funds(config), "funds")
    urgent, feed_health = build_urgent(stocks)
    if not urgent:
        urgent = [
            item for item in previous.get("urgent", [])
            if item.get("expiresAt", "") > datetime.now(MOSCOW_TZ).isoformat()
        ]

    payload = {
        "generatedAt": now_iso(),
        "methodologyVersion": config["methodologyVersion"],
        "market": "Московская биржа",
        "urgent": urgent,
        "stocks": stocks,
        "bonds": bonds,
        "funds": funds,
        "macro": config["macro"],
        "fundModel": config["funds"]["model"],
        "sourceHealth": status + feed_health,
        "disclaimer": "Информация не является индивидуальной инвестиционной рекомендацией. Прогнозы и дивиденды не гарантированы.",
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Снимок записан: {OUTPUT_PATH} ({payload['generatedAt']})")
    failed = [item for item in status if item["status"] == "error"]
    return 1 if failed and not (stocks or bonds or funds) else 0


if __name__ == "__main__":
    sys.exit(main())
