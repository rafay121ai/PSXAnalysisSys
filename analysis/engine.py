"""Five-framework stock analysis and verdict generation."""

import json
import logging
from statistics import mean
from typing import Any

from openai import OpenAI

from config import (
    OPENAI_API_KEY,
    OPENAI_MODEL,
    TIER1_FRAMEWORK_THRESHOLD,
    TIER1_REWARD_RISK,
    TIER2_FRAMEWORK_THRESHOLD,
    TIER2_REWARD_RISK,
)

LOGGER = logging.getLogger(__name__)

MAJOR_CATALYSTS = ("contract", "acquisition", "merger", "record profit", "rights issue")
MINOR_CATALYSTS = ("earnings", "profit", "dividend", "management", "appointment", "expansion")
NEGATIVE_TERMS = ("loss", "decline", "default", "fraud", "penalty", "downgrade")


def _closes(stock: dict[str, Any]) -> list[float]:
    """Return chronological closing prices from newest-first PSX history."""
    return [float(row["close"]) for row in reversed(stock.get("price_history", [])) if row.get("close")]


def _volumes(stock: dict[str, Any]) -> list[float]:
    """Return chronological volumes from newest-first PSX history."""
    return [float(row["volume"]) for row in reversed(stock.get("price_history", [])) if row.get("volume") is not None]


def _moving_average(values: list[float], periods: int) -> float | None:
    """Calculate a simple moving average when enough observations exist."""
    return mean(values[-periods:]) if len(values) >= periods else None


def weinstein_analysis(stock: dict[str, Any]) -> dict[str, Any]:
    """Estimate Weinstein stage from price, 30-week MA slope, and volume."""
    closes, volumes = _closes(stock), _volumes(stock)
    ma_150 = _moving_average(closes, 150)
    older_ma = mean(closes[-170:-20]) if len(closes) >= 170 else None
    price = float(stock.get("current_price") or (closes[-1] if closes else 0))
    recent_volume = _moving_average(volumes, 20)
    prior_volume = mean(volumes[-40:-20]) if len(volumes) >= 40 else None
    rising_ma = ma_150 is not None and older_ma is not None and ma_150 > older_ma
    rising_volume = recent_volume is not None and prior_volume is not None and recent_volume > prior_volume

    if ma_150 is None:
        stage = 0
        reason = "Insufficient history for a 30-week moving average"
    elif price > ma_150 and rising_ma:
        stage = 2
        reason = "Price is above a rising 30-week MA"
    elif price < ma_150 and not rising_ma:
        stage = 4
        reason = "Price is below a flat or falling 30-week MA"
    elif price > ma_150:
        stage = 1
        reason = "Price is above a non-rising 30-week MA"
    else:
        stage = 3
        reason = "Price is below a still-rising 30-week MA"
    return {"stage": stage, "rising_volume": rising_volume, "reason": reason, "passed": stage == 2}


def minervini_analysis(stock: dict[str, Any]) -> dict[str, Any]:
    """Evaluate five simplified Minervini SEPA trend-template criteria."""
    closes = _closes(stock)
    price = float(stock.get("current_price") or (closes[-1] if closes else 0))
    ma_50, ma_200 = _moving_average(closes, 50), _moving_average(closes, 200)
    old_ma_200 = mean(closes[-220:-20]) if len(closes) >= 220 else None
    high = stock.get("52_week_high")
    low = stock.get("52_week_low")
    rs_rating = stock.get("rs_rating")
    if rs_rating is None and high is not None and low is not None and high != low:
        rs_rating = max(0, min(100, 100 * (price - low) / (high - low)))
    criteria = {
        "above_50_day_ma": ma_50 is not None and price > ma_50,
        "above_200_day_ma": ma_200 is not None and price > ma_200,
        "200_day_ma_trending_up": ma_200 is not None and old_ma_200 is not None and ma_200 > old_ma_200,
        "within_25_percent_of_high": high is not None and price >= float(high) * 0.75,
        "rs_rating_above_70": rs_rating is not None and float(rs_rating) > 70,
    }
    count = sum(criteria.values())
    return {"criteria": criteria, "criteria_passed": count, "rs_rating": rs_rating, "passed": count >= 3}


def _heuristic_catalyst(articles: list[dict[str, Any]]) -> dict[str, Any]:
    """Score company news catalysts with transparent keyword rules."""
    text = " ".join(f"{item.get('headline', '')} {item.get('full_text', '')}" for item in articles).lower()
    major = [term for term in MAJOR_CATALYSTS if term in text]
    minor = [term for term in MINOR_CATALYSTS if term in text]
    score = 2 if major else 1 if minor else 0
    matches = major or minor
    return {"score": score, "summary": f"Matched catalyst terms: {', '.join(matches)}" if matches else "No clear catalyst", "method": "keywords"}


def catalyst_analysis(
    stock: dict[str, Any],
    news: list[dict[str, Any]],
    use_ai: bool = True,
    announcements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Use GPT-5.4 to classify relevant news and PSX announcement catalysts."""
    symbol = stock["symbol"]
    relevant_news = [item for item in news if symbol in item.get("mentioned_symbols", [])]
    relevant_announcements = [
        item for item in announcements or [] if item.get("symbol") == symbol
    ]
    relevant = relevant_news + [
        {
            "headline": item.get("title", ""),
            "full_text": item.get("announcement_type", ""),
            "source": "PSX announcement",
        }
        for item in relevant_announcements
    ]
    fallback = _heuristic_catalyst(relevant)
    if not relevant or not use_ai or not OPENAI_API_KEY:
        return {
            **fallback,
            "articles_considered": len(relevant_news),
            "announcements_considered": len(relevant_announcements),
            "passed": fallback["score"] >= 1,
        }
    prompt = {
        "symbol": symbol,
        "company": stock.get("company_name"),
        "articles": [
            {"headline": item.get("headline"), "text": item.get("full_text", "")[:500]}
            for item in relevant[:8]
        ],
        "instruction": "Score catalyst 0=no catalyst, 1=minor, 2=major. Return JSON with integer score and short summary.",
    }
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=json.dumps(prompt),
        )
        result = json.loads(response.output_text)
        score = max(0, min(2, int(result.get("score", 0))))
        return {
            "score": score,
            "summary": str(result.get("summary", ""))[:240],
            "method": OPENAI_MODEL,
            "articles_considered": len(relevant_news),
            "announcements_considered": len(relevant_announcements),
            "passed": score >= 1,
        }
    except Exception as exc:
        LOGGER.warning("GPT catalyst analysis failed for %s: %s", symbol, exc)
        return {
            **fallback,
            "articles_considered": len(relevant_news),
            "announcements_considered": len(relevant_announcements),
            "passed": fallback["score"] >= 1,
        }


def kelly_analysis(win_rate: float = 0.55, avg_win_loss_ratio: float = 1.8) -> dict[str, Any]:
    """Calculate a capped full-Kelly portfolio allocation."""
    raw_kelly = (
        win_rate - ((1 - win_rate) / avg_win_loss_ratio)
        if avg_win_loss_ratio > 0
        else 0
    )
    position = max(0.0, min(0.25, raw_kelly))
    return {
        "win_rate": win_rate,
        "avg_win_loss_ratio": avg_win_loss_ratio,
        "win_loss_ratio": avg_win_loss_ratio,
        "raw_kelly": raw_kelly,
        "position_size": position,
        "passed": position > 0.05,
    }


def munger_inversion(stock: dict[str, Any], news: list[dict[str, Any]], catalyst_score: int) -> dict[str, Any]:
    """Count explicit reasons not to buy the stock."""
    symbol = stock["symbol"]
    relevant_text = " ".join(
        f"{item.get('headline', '')} {item.get('full_text', '')}"
        for item in news
        if symbol in item.get("mentioned_symbols", [])
    ).lower()
    volumes = _volumes(stock)
    price, high = stock.get("current_price"), stock.get("52_week_high")
    flags = {
        "high_debt": float(stock.get("debt_ratio", 0) or 0) > 1.0,
        "declining_volume": len(volumes) >= 40 and mean(volumes[-20:]) < mean(volumes[-40:-20]),
        "negative_news": any(term in relevant_text for term in NEGATIVE_TERMS),
        "price_near_52_week_high": price is not None and high is not None and float(price) >= float(high) * 0.95,
        "no_catalyst": catalyst_score == 0,
    }
    red_flags = [name for name, flagged in flags.items() if flagged]
    return {"red_flags": red_flags, "score": len(red_flags), "passed": len(red_flags) <= 2}


def _risk_levels(stock: dict[str, Any]) -> tuple[float, float, float]:
    """Calculate stop, target, and reward-risk from current price and range."""
    price = float(stock.get("current_price") or 0)
    closes = _closes(stock)
    recent_support = min(closes[-20:]) if closes else price * 0.92
    stop = min(price * 0.92, recent_support)
    risk = max(price - stop, price * 0.01)
    high = float(stock.get("52_week_high") or 0)
    target = max(high, price + (2 * risk))
    return round(stop, 2), round(target, 2), round((target - price) / risk, 2)


def analyze_stock(
    stock: dict[str, Any],
    news: list[dict[str, Any]],
    use_ai: bool = True,
    announcements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run all five frameworks and return a structured recommendation."""
    weinstein = weinstein_analysis(stock)
    minervini = minervini_analysis(stock)
    catalyst = catalyst_analysis(stock, news, use_ai=use_ai, announcements=announcements)
    kelly = kelly_analysis()
    munger = munger_inversion(stock, news, catalyst["score"])
    details = {
        "weinstein": weinstein,
        "minervini": minervini,
        "catalyst": catalyst,
        "kelly": kelly,
        "munger": munger,
    }
    passed = sum(bool(detail["passed"]) for detail in details.values())
    stop, target, reward_risk = _risk_levels(stock)
    if passed >= TIER1_FRAMEWORK_THRESHOLD and reward_risk >= TIER1_REWARD_RISK:
        tier, verdict = 1, "STRONG BUY"
    elif passed >= TIER2_FRAMEWORK_THRESHOLD and reward_risk >= TIER2_REWARD_RISK:
        tier, verdict = 2, "SPECULATIVE BUY"
    else:
        tier, verdict = 0, "SKIP"
    confidence = round((passed / 5) * float(stock.get("data_quality", 0)) * 100, 1)
    return {
        "symbol": stock["symbol"],
        "company_name": stock.get("company_name", stock["symbol"]),
        "current_price": stock.get("current_price"),
        "tier": tier,
        "frameworks_passed": passed,
        "framework_details": details,
        "kelly_position_size": round(kelly["position_size"] * 100, 2),
        "stop_loss": stop,
        "target_price": target,
        "reward_risk": reward_risk,
        "confidence_score": confidence,
        "verdict_text": verdict,
    }


def analyze_all(
    stocks: list[dict[str, Any]],
    news: list[dict[str, Any]],
    use_ai: bool = True,
    announcements: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Analyze every stock and sort actionable picks ahead of skipped stocks."""
    results = [
        analyze_stock(stock, news, use_ai=use_ai, announcements=announcements)
        for stock in stocks
    ]
    return sorted(results, key=lambda item: (item["tier"] == 0, item["tier"], -item["confidence_score"]))
