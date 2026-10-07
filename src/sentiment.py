"""
TRIO — Sentiment Analysis

Fetches financial news and social media posts, scores sentiment using
VADER (lightweight) or FinBERT (accurate), and aggregates into a single
score from -1 (very bearish) to +1 (very bullish).

Results are cached to avoid redundant API calls.

DISCLAIMER: Educational purposes only. Not financial advice.
"""

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from src.utils import get_env, get_logger, load_config, retry_with_backoff, utc_now, hours_ago

logger = get_logger("sentiment")

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_sentiment_cache: Dict[str, Dict[str, Any]] = {}


def _cache_key(symbol: str) -> str:
    return symbol.upper().strip()


def _is_cached(symbol: str, ttl_minutes: int) -> bool:
    key = _cache_key(symbol)
    if key not in _sentiment_cache:
        return False
    cached_at = _sentiment_cache[key].get("cached_at", 0)
    return (time.time() - cached_at) < (ttl_minutes * 60)


def _get_cached(symbol: str) -> Optional[Dict[str, Any]]:
    key = _cache_key(symbol)
    return _sentiment_cache.get(key)


def _set_cache(symbol: str, data: Dict[str, Any]) -> None:
    key = _cache_key(symbol)
    data["cached_at"] = time.time()
    _sentiment_cache[key] = data


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SentimentSource:
    """A single sentiment data point from one source."""
    source: str = ""          # newsapi, reddit, finnhub
    text: str = ""            # headline or post
    score: float = 0.0        # -1 to +1
    age_hours: float = 0.0    # how old is this data
    weight: float = 1.0       # source reliability weight

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SentimentResult:
    """Aggregated sentiment result for a symbol."""
    symbol: str = ""
    score: float = 0.0        # -1 to +1 weighted aggregate
    label: str = "neutral"    # very_negative, negative, neutral, positive, very_positive
    sources: List[SentimentSource] = field(default_factory=list)
    cached: bool = False
    computed_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "score": self.score,
            "label": self.label,
            "sources": [s.to_dict() for s in self.sources],
            "cached": self.cached,
            "computed_at": self.computed_at,
        }


# ---------------------------------------------------------------------------
# Scoring engines
# ---------------------------------------------------------------------------

def _score_vader(text: str) -> float:
    """Score text sentiment using VADER. Returns -1 to +1."""
    try:
        from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
    except ImportError:
        raise ImportError("Install vaderSentiment: pip install vaderSentiment")

    analyzer = SentimentIntensityAnalyzer()
    scores = analyzer.polarity_scores(text)
    return scores["compound"]  # already -1 to +1


def _score_finbert(text: str) -> float:
    """Score text sentiment using FinBERT. Returns -1 to +1."""
    try:
        from transformers import pipeline
    except ImportError:
        raise ImportError("Install transformers and torch for FinBERT support")

    # Use singleton pattern to avoid reloading model
    if not hasattr(_score_finbert, "_pipeline"):
        logger.info("Loading FinBERT model (first time, may take a moment)...")
        _score_finbert._pipeline = pipeline(
            "sentiment-analysis",
            model="ProsusAI/finbert",
            tokenizer="ProsusAI/finbert",
        )

    result = _score_finbert._pipeline(text[:512])[0]  # truncate to model max
    label = result["label"].lower()
    score_val = result["score"]

    if label == "positive":
        return score_val
    elif label == "negative":
        return -score_val
    else:
        return 0.0


def _score_text(text: str, model: str = "vader") -> float:
    """Score text using the configured model."""
    if model == "finbert":
        return _score_finbert(text)
    return _score_vader(text)


# ---------------------------------------------------------------------------
# News fetchers
# ---------------------------------------------------------------------------

@retry_with_backoff(max_retries=2, backoff_base=2.0)
def _fetch_newsapi(symbol: str, max_articles: int = 10) -> List[Dict[str, Any]]:
    """Fetch news headlines from NewsAPI.org."""
    api_key = get_env("NEWSAPI_KEY")
    if not api_key:
        logger.warning("NEWSAPI_KEY not set, skipping NewsAPI")
        return []

    # Clean symbol for search (remove exchange suffix like .NS)
    query = symbol.split(".")[0]

    url = "https://newsapi.org/v2/everything"
    params = {
        "q": query,
        "language": "en",
        "sortBy": "publishedAt",
        "pageSize": max_articles,
        "apiKey": api_key,
    }

    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    articles = []
    for art in data.get("articles", []):
        articles.append({
            "title": art.get("title", ""),
            "published_at": art.get("publishedAt", ""),
            "source": art.get("source", {}).get("name", "unknown"),
        })

    logger.info("NewsAPI: fetched %d articles for '%s'", len(articles), query)
    return articles


@retry_with_backoff(max_retries=2, backoff_base=2.0)
def _fetch_finnhub(symbol: str) -> List[Dict[str, Any]]:
    """Fetch news from Finnhub."""
    api_key = get_env("FINNHUB_API_KEY")
    if not api_key:
        logger.warning("FINNHUB_API_KEY not set, skipping Finnhub")
        return []

    from datetime import datetime, timedelta

    end = datetime.now()
    start = end - timedelta(days=2)

    url = "https://finnhub.io/api/v1/company-news"
    params = {
        "symbol": symbol.split(".")[0],
        "from": start.strftime("%Y-%m-%d"),
        "to": end.strftime("%Y-%m-%d"),
        "token": api_key,
    }

    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    news = resp.json()

    articles = []
    for item in news[:15]:
        articles.append({
            "title": item.get("headline", ""),
            "published_at": item.get("datetime", ""),
            "source": item.get("source", "finnhub"),
        })

    logger.info("Finnhub: fetched %d articles for '%s'", len(articles), symbol)
    return articles


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def _score_to_label(score: float) -> str:
    """Convert numeric score to human-readable label."""
    if score >= 0.5:
        return "very_positive"
    elif score >= 0.15:
        return "mildly_positive"
    elif score <= -0.5:
        return "very_negative"
    elif score <= -0.15:
        return "mildly_negative"
    return "neutral"


def _compute_weighted_score(
    sources: List[SentimentSource],
    recency_decay: float = 0.1,
) -> float:
    """
    Weighted average of source scores, decayed by recency.

    Weight = source_reliability_weight * exp(-recency_decay * age_hours)
    """
    import math

    if not sources:
        return 0.0

    total_weight = 0.0
    weighted_sum = 0.0

    for src in sources:
        decay = math.exp(-recency_decay * src.age_hours)
        w = src.weight * decay
        weighted_sum += src.score * w
        total_weight += w

    if total_weight == 0:
        return 0.0

    return round(weighted_sum / total_weight, 4)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_sentiment(
    symbol: str,
    config_override: Optional[Dict[str, Any]] = None,
) -> SentimentResult:
    """
    Analyze sentiment for a symbol using configured news/social sources.

    Args:
        symbol:          Ticker symbol.
        config_override: Override sentiment config section.

    Returns:
        SentimentResult with aggregated score and source details.
    """
    cfg = load_config()
    sent_cfg = config_override or cfg.get("sentiment", {})

    if not sent_cfg.get("enabled", True):
        logger.info("Sentiment analysis disabled in config")
        return SentimentResult(symbol=symbol, computed_at=utc_now())

    # Check cache
    cache_ttl = sent_cfg.get("cache_ttl_minutes", 15)
    if _is_cached(symbol, cache_ttl):
        cached = _get_cached(symbol)
        logger.info("Using cached sentiment for %s", symbol)
        result = SentimentResult(
            symbol=symbol,
            score=cached.get("score", 0.0),
            label=cached.get("label", "neutral"),
            cached=True,
            computed_at=cached.get("computed_at", ""),
        )
        return result

    model = sent_cfg.get("model", "vader")
    source_weights = sent_cfg.get("source_weights", {})
    recency_decay = sent_cfg.get("recency_decay", 0.1)

    all_sources: List[SentimentSource] = []

    # --- NewsAPI ---
    newsapi_cfg = sent_cfg.get("sources", {}).get("newsapi", {})
    if newsapi_cfg.get("enabled", False):
        try:
            articles = _fetch_newsapi(
                symbol,
                max_articles=newsapi_cfg.get("max_articles", 10),
            )
            for art in articles:
                title = art.get("title", "")
                if not title:
                    continue
                score = _score_text(title, model)
                age = 0.0
                pub = art.get("published_at", "")
                if pub:
                    try:
                        age = hours_ago(pub)
                    except Exception:
                        age = 12.0

                max_age = newsapi_cfg.get("max_age_hours", 24)
                if age <= max_age:
                    all_sources.append(SentimentSource(
                        source="newsapi",
                        text=title[:200],
                        score=score,
                        age_hours=round(age, 1),
                        weight=source_weights.get("newsapi", 0.8),
                    ))
        except Exception as exc:
            logger.error("NewsAPI fetch failed: %s", exc)

    # --- Finnhub ---
    finnhub_cfg = sent_cfg.get("sources", {}).get("finnhub", {})
    if finnhub_cfg.get("enabled", False):
        try:
            articles = _fetch_finnhub(symbol)
            for art in articles:
                title = art.get("title", "")
                if not title:
                    continue
                score = _score_text(title, model)
                all_sources.append(SentimentSource(
                    source="finnhub",
                    text=title[:200],
                    score=score,
                    age_hours=0.0,
                    weight=source_weights.get("finnhub", 0.7),
                ))
        except Exception as exc:
            logger.error("Finnhub fetch failed: %s", exc)

    # --- Aggregate ---
    agg_score = _compute_weighted_score(all_sources, recency_decay)
    label = _score_to_label(agg_score)

    result = SentimentResult(
        symbol=symbol,
        score=agg_score,
        label=label,
        sources=all_sources,
        cached=False,
        computed_at=utc_now(),
    )

    # Cache it
    _set_cache(symbol, {"score": agg_score, "label": label, "computed_at": result.computed_at})

    logger.info("Sentiment for %s: score=%.3f label=%s (%d sources)", symbol, agg_score, label, len(all_sources))

    return result
