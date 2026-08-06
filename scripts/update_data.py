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
    }
    if securities:
        query["securities"] = ",".join(securities)
    static: dict[str, dict[str, Any]] = {}
    dynamic: dict[str, dict[str, Any]] = {}
    start = 0
    while True:
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
        if securities or max(len(static_page), len(dynamic_page)) < 100:
            break
        start += 100
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
    return sorted(
        output,
        key=lambda item: (item["expectedReturn"], item["confidence"]),
        reverse=True,
    )[:RANKING_LIMIT]


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
    return sorted(
        result,
        key=lambda item: (item["expectedReturn"], item["confidence"]),
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
        context = ("акци", "компан", "эмитент", "дивид", "выруч", "прибыл", "отчет", "ритейл")
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


def related_instrument(
    text: str,
    stocks: list[dict[str, Any]],
    bonds: list[dict[str, Any]],
    funds: list[dict[str, Any]] | None = None,
) -> tuple[str, list[str], float] | None:
    upper = text.upper()
    lower = text.lower()
    known_tickers = {stock["secid"] for stock in stocks} | set(ISSUER_TICKERS.values())
    fund_tickers = {fund["secid"] for fund in (funds or [])}
    isin = re.search(r"\bRU[A-Z0-9]{10}\b", upper)
    if isin:
        return isin.group(0), [f"#{isin.group(0)}"], 0.99
    tagged_tickers = re.findall(r"#([A-Z]{4,5})\b", upper)
    for ticker in tagged_tickers:
        if ticker in fund_tickers or ticker in NON_TICKER_TOKENS:
            continue
        if ticker in known_tickers or "🇷🇺" in text:
            company_name = ENTITY_BY_SECID.get(ticker, {}).get("name", ticker)
            confidence = 0.98 if ticker in known_tickers else 0.65
            return ticker, list(dict.fromkeys([make_hashtag(company_name), f"#{ticker}"])), confidence
    for stock in stocks:
        ticker_match = re.search(rf"\b{re.escape(stock['secid'])}\b", upper)
        if ticker_match or stock["name"].upper().split(",")[0] in upper:
            tags = [make_hashtag(stock["name"].split(",")[0]), f"#{stock['secid']}"]
            confidence = 0.99 if ticker_match else 0.95
            return stock["secid"], list(dict.fromkeys(tag for tag in tags if tag)), confidence
    for bond in bonds:
        if bond["secid"] in upper or str(bond["name"]).lower() in lower:
            tags = [make_hashtag(str(bond["name"])), f"#{bond['secid']}"]
            confidence = 0.99 if bond["secid"] in upper else 0.9
            return bond["secid"], list(dict.fromkeys(tag for tag in tags if tag)), confidence
    for issuer, ticker_id in sorted(ISSUER_TICKERS.items(), key=lambda item: len(item[0]), reverse=True):
        if alias_matches(issuer, lower):
            entity = ENTITY_BY_SECID[ticker_id]
            return ticker_id, [make_hashtag(entity["name"]), f"#{ticker_id}"], 0.9
    if re.search(r"акци|бумаг|облигац|тикер", lower):
        tickers = re.findall(r"\b[A-Z]{4,5}\b", upper)
        ticker = next((value for value in tickers if value not in NON_TICKER_TOKENS), None)
        if ticker:
            return ticker, [f"#{ticker}"], 0.6
    return None


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
    event_groups = {
        "credit_distress": ("дефолт", "банкрот", "реструктуризац", "просроч"),
        "legal": ("арест", "обыск", "расследован", "задержан", "обвинен"),
        "trading_restriction": ("приостанов", "прекращение торгов", "дискретный аукцион", "режим д"),
        "sanctions": ("санкци",),
        "report": ("прибыл", "выруч", "отчет", "результат"),
        "dividend": ("дивиденд",),
        "corporate_action": ("сделк", "оферт", "поглощен", "выкуп"),
    }
    for group, markers in event_groups.items():
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
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, Any]]:
    started_at = time.perf_counter()
    health = []
    news: list[dict[str, str]] = []
    try:
        items = moex_sitenews()
        news.extend(items)
        health.append({"source": "Московская биржа", "status": "ok", "detail": f"{len(items)} сообщений"})
    except Exception as exc:
        health.append({"source": "Московская биржа", "status": "error", "detail": str(exc)[:140]})
    with ThreadPoolExecutor(max_workers=6) as executor:
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
        instrument = related_instrument(item["title"], stocks, bonds, funds)
        if instrument is None:
            metrics["unlinked"] += 1
            continue
        ticker, hashtags, entity_confidence = instrument
        direction = "SELL" if negative > positive else "BUY"
        if direction == "SELL" and is_negative_actor_only(item["title"], ticker):
            metrics["unlinked"] += 1
            continue
        source = {"publisher": item["source"], "url": item["url"]}
        priority = SOURCE_PRIORITY.get(item["source"], 1)
        event = event_key(text, direction)
        sentiment, impact, impact_confidence = impact_estimate(
            direction,
            strength,
            entity_confidence,
            priority,
        )
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
            "_event": event,
            "_published": published,
            "_tokens": text_shingles(item["title"]),
        }
        existing = next(
            (
                signal for signal in signal_clusters
                if signal["ticker"] == ticker
                and signal["action"] == direction
                and (
                    jaccard_similarity(signal["_tokens"], candidate["_tokens"]) >= 0.52
                    or (
                        signal["_event"] == event
                        and abs((signal["_published"] - published).total_seconds()) <= 129_600
                    )
                )
            ),
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
            ):
                existing[field] = candidate[field]

    signals = signal_clusters
    for signal in signals:
        for internal_field in ("_priority", "_event", "_published", "_tokens"):
            signal.pop(internal_field, None)
    matched_documents = len(signals) + metrics["duplicatesMerged"]
    metrics.update({
        "signals": len(signals),
        "dedupRate": round(metrics["duplicatesMerged"] / matched_documents, 3) if matched_documents else 0.0,
        "impactAccuracy": None,
        "latencyMs": round((time.perf_counter() - started_at) * 1000),
    })
    metrics.update(evaluate_entity_linking(stocks, bonds, funds))
    return sorted(
        signals,
        key=lambda item: (item["strength"], item["publishedAt"]),
        reverse=True,
    )[:URGENT_LIMIT], health, metrics


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
    urgent, feed_health, pipeline_metrics = build_urgent(stocks, bonds, funds)
    if not urgent:
        urgent = [
            item for item in previous.get("urgent", [])
            if item.get("expiresAt", "") > datetime.now(MOSCOW_TZ).isoformat()
            and item.get("ticker") not in {"РЫНОК", "НЕФТЕГАЗ"}
            and item.get("hashtags")
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
        "pipelineMetrics": pipeline_metrics,
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
