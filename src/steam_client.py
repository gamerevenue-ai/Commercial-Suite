# src/steam_client.py
# Steam data ingestion layer.
#
# Data sources:
#   [official]     Steam Web API — store.steampowered.com/api + api.steampowered.com
#   [third-party]  SteamCharts — steamcharts.com (fragile scrape, optional enrichment)
#
# All responses are cached locally (data/cache/) to avoid re-fetching and
# to stay within Steam's undocumented rate limits (~200 req / 5 min).
#
# LEGAL NOTE: Steam Web API is freely accessible for non-commercial use.
# SteamCharts scraping is unofficial and may break without notice.
# Do not hammer either source — respect rate limits.

import json
import time
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Endpoint constants
# ---------------------------------------------------------------------------
_APPDETAILS_URL   = "https://store.steampowered.com/api/appdetails"
_REVIEWS_URL      = "https://store.steampowered.com/appreviews/{appid}"
_CCU_URL          = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
_SEARCH_URL       = "https://store.steampowered.com/api/storesearch/"
_STEAMCHARTS_URL  = "https://steamcharts.com/app/{appid}/chart-data.json"

_CACHE_DIR = Path("data/cache")
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}


class SteamAPIError(Exception):
    """Raised when a Steam API call fails unrecoverably."""


class SteamClient:
    """
    Thin wrapper around Steam public APIs with local disk caching.

    Parameters
    ----------
    cache_ttl_hours : int
        How long cached responses are considered fresh. Default 24h.
    rate_limit_sleep : float
        Seconds to sleep between outbound requests. Default 0.6s.
    """

    def __init__(self, cache_ttl_hours: int = 24, rate_limit_sleep: float = 0.6):
        self.cache_ttl = cache_ttl_hours * 3600
        self.sleep = rate_limit_sleep
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------------
    # Cache helpers
    # -----------------------------------------------------------------------

    def _cache_path(self, key: str) -> Path:
        digest = hashlib.md5(key.encode()).hexdigest()
        return _CACHE_DIR / f"{digest}.json"

    def _read_cache(self, key: str):
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            if time.time() - envelope["ts"] > self.cache_ttl:
                return None
            return envelope["value"]
        except Exception:
            return None

    def _write_cache(self, key: str, value) -> None:
        path = self._cache_path(key)
        path.write_text(
            json.dumps({"ts": time.time(), "value": value}),
            encoding="utf-8",
        )

    def clear_cache(self, appid: int | None = None) -> None:
        """Delete cached files. If appid given, only that game's entries."""
        if appid is None:
            for f in _CACHE_DIR.glob("*.json"):
                f.unlink()
        else:
            # Brute-force: regenerate keys we know about for this appid
            known_prefixes = [
                f"appdetails_{appid}",
                f"review_summary_{appid}",
                f"steamcharts_{appid}",
            ]
            for prefix in known_prefixes:
                path = self._cache_path(prefix)
                if path.exists():
                    path.unlink()

    # -----------------------------------------------------------------------
    # HTTP helper
    # -----------------------------------------------------------------------

    def _get(self, url: str, params: dict | None = None, cache_key: str | None = None,
             headers: dict | None = None, skip_cache: bool = False) -> dict | list:
        if cache_key and not skip_cache:
            cached = self._read_cache(cache_key)
            if cached is not None:
                logger.debug("Cache hit: %s", cache_key)
                return cached

        time.sleep(self.sleep)
        try:
            r = requests.get(
                url,
                params=params,
                headers=headers or _DEFAULT_HEADERS,
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
        except requests.exceptions.HTTPError as e:
            raise SteamAPIError(f"HTTP {e.response.status_code} from {url}") from e
        except requests.exceptions.RequestException as e:
            raise SteamAPIError(f"Request failed for {url}: {e}") from e
        except ValueError as e:
            raise SteamAPIError(f"JSON decode failed for {url}: {e}") from e

        if cache_key and not skip_cache:
            self._write_cache(cache_key, data)

        return data

    # -----------------------------------------------------------------------
    # Public methods — official Steam API
    # -----------------------------------------------------------------------

    def get_app_details(self, appid: int) -> dict:
        """
        Fetch game metadata from the Steam store API.

        Returns the 'data' sub-object from the appdetails response.
        Source: [official] store.steampowered.com/api/appdetails

        Raises SteamAPIError if the appid is not found or the API fails.
        """
        cache_key = f"appdetails_{appid}"
        data = self._get(
            _APPDETAILS_URL,
            params={"appids": appid, "cc": "us", "l": "en"},
            cache_key=cache_key,
        )
        key = str(appid)
        if not data.get(key, {}).get("success"):
            raise SteamAPIError(
                f"App {appid} not found or Steam returned success=false."
            )
        return data[key]["data"]

    def get_review_summary(self, appid: int) -> dict:
        """
        Fetch aggregate review counts and sentiment from the reviews endpoint.

        Returns the query_summary dict:
            num_reviews, review_score, review_score_desc,
            total_positive, total_negative, total_reviews

        Source: [official] store.steampowered.com/appreviews/{appid}
        """
        cache_key = f"review_summary_{appid}"
        data = self._get(
            _REVIEWS_URL.format(appid=appid),
            params={
                "json": 1,
                "language": "all",
                "num_per_page": 0,
                "purchase_type": "all",
                "review_type": "all",
            },
            cache_key=cache_key,
        )
        summary = data.get("query_summary", {})
        if not summary:
            raise SteamAPIError(f"No review summary returned for appid {appid}.")
        return summary

    def get_review_timeseries(
        self,
        appid: int,
        lookback_months: int = 24,
        max_reviews: int = 5000,
    ) -> list[dict]:
        """
        Paginate through reviews to reconstruct a time series of review events.

        Each item: {"timestamp": <unix int>, "voted_up": <bool>}
        Items are sorted newest-first as returned by Steam.

        Stops when either:
          - We've collected max_reviews items, OR
          - The next review is older than lookback_months

        Source: [official] store.steampowered.com/appreviews (paginated)

        PERFORMANCE NOTE: Large games (>10k reviews in window) hit the
        max_reviews cap and sample only the most recent N reviews.
        The resulting curve is still usable for shape-fitting but the
        total count is truncated — use review_summary for totals.
        """
        cache_key = f"review_ts_{appid}_{lookback_months}_{max_reviews}"
        cached = self._read_cache(cache_key)
        if cached is not None:
            return cached

        cutoff_ts = (
            datetime.now(timezone.utc) - timedelta(days=lookback_months * 30.44)
        ).timestamp()

        url = _REVIEWS_URL.format(appid=appid)
        cursor = "*"
        reviews: list[dict] = []
        pages_fetched = 0

        while len(reviews) < max_reviews:
            params = {
                "json": 1,
                "language": "all",
                "filter": "all",
                "review_type": "all",
                "purchase_type": "all",
                "num_per_page": 100,
                "cursor": cursor,
            }
            try:
                data = self._get(url, params=params)
            except SteamAPIError as e:
                logger.warning("Review pagination interrupted: %s", e)
                break

            batch = data.get("reviews", [])
            if not batch:
                break

            reached_cutoff = False
            for r in batch:
                ts = r.get("timestamp_created", 0)
                if ts < cutoff_ts:
                    reached_cutoff = True
                    break
                reviews.append({"timestamp": ts, "voted_up": r.get("voted_up", True)})

            if reached_cutoff:
                break

            new_cursor = data.get("cursor", "")
            if not new_cursor or new_cursor == cursor:
                break
            cursor = new_cursor
            pages_fetched += 1
            time.sleep(self.sleep)

        logger.info(
            "Fetched %d reviews for appid %d (%d pages)",
            len(reviews), appid, pages_fetched,
        )
        self._write_cache(cache_key, reviews)
        return reviews

    def get_current_ccu(self, appid: int) -> int:
        """
        Get the current (live) concurrent player count.

        Source: [official] ISteamUserStats/GetNumberOfCurrentPlayers
        NOTE: This is a snapshot, not historical. Low confidence for unit estimation.
        Cache key includes the current hour so it refreshes hourly.
        """
        hour_bucket = int(time.time() // 3600)
        cache_key = f"ccu_{appid}_{hour_bucket}"
        data = self._get(
            _CCU_URL,
            params={"appid": appid},
            cache_key=cache_key,
        )
        return data.get("response", {}).get("player_count", 0)

    def search_games(self, query: str, max_results: int = 10) -> list[dict]:
        """
        Search Steam store for games matching a text query.

        Returns list of dicts: [{appid, name, price}]
        Source: [official] store.steampowered.com/api/storesearch
        """
        data = self._get(
            _SEARCH_URL,
            params={"term": query, "l": "en", "cc": "US"},
        )
        items = data.get("items", [])[:max_results]
        results = []
        for item in items:
            if item.get("type") != "app":
                continue
            price_block = item.get("price", {})
            results.append({
                "appid": item["id"],
                "name": item.get("name", ""),
                "price_usd": price_block.get("final", 0) / 100 if price_block else None,
            })
        return results

    # -----------------------------------------------------------------------
    # Public methods — third-party scrape
    # -----------------------------------------------------------------------

    def get_steamcharts_history(self, appid: int) -> list[dict]:
        """
        Fetch monthly average CCU history from SteamCharts.

        Returns list of dicts: [{"year_month": "YYYY-MM", "avg_players": N, "peak_players": N}]
        Sorted oldest-first.

        SOURCE: [third-party scrape] steamcharts.com — FRAGILE.
        This endpoint is not officially supported and may break without notice.
        Returns empty list on any failure (non-fatal).

        The raw response is a list of [timestamp_ms, avg_players] pairs.
        """
        cache_key = f"steamcharts_{appid}"
        cached = self._read_cache(cache_key)
        if cached is not None:
            return cached

        url = _STEAMCHARTS_URL.format(appid=appid)
        try:
            time.sleep(self.sleep)
            r = requests.get(url, headers=_DEFAULT_HEADERS, timeout=15)
            r.raise_for_status()
            raw = r.json()
        except Exception as e:
            logger.warning("SteamCharts fetch failed for %d: %s", appid, e)
            return []

        # raw is list of [timestamp_ms, avg_players] — convert to named dicts
        results = []
        for entry in raw:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            ts_ms = entry[0]
            avg = entry[1]
            if ts_ms is None or avg is None:
                continue
            dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            results.append({
                "year_month": dt.strftime("%Y-%m"),
                "avg_players": avg,
            })

        results.sort(key=lambda x: x["year_month"])
        self._write_cache(cache_key, results)
        return results

    # -----------------------------------------------------------------------
    # Convenience: fetch everything for one appid in one call
    # -----------------------------------------------------------------------

    def get_full_profile(
        self,
        appid: int,
        lookback_months: int = 24,
        max_reviews: int = 5000,
        include_steamcharts: bool = True,
    ) -> dict:
        """
        Fetch all available signals for one appid.

        Returns a dict with keys:
            appid, app_details, review_summary, review_timeseries,
            current_ccu, steamcharts_history, fetch_errors

        Individual failures are recorded in fetch_errors rather than raising,
        so a partial profile is still usable.
        """
        profile: dict = {
            "appid": appid,
            "app_details": None,
            "review_summary": None,
            "review_timeseries": [],
            "current_ccu": 0,
            "steamcharts_history": [],
            "fetch_errors": [],
        }

        # App details (required — raises on failure)
        try:
            profile["app_details"] = self.get_app_details(appid)
        except SteamAPIError as e:
            profile["fetch_errors"].append(f"app_details: {e}")
            return profile  # Can't proceed without basic metadata

        # Review summary
        try:
            profile["review_summary"] = self.get_review_summary(appid)
        except SteamAPIError as e:
            profile["fetch_errors"].append(f"review_summary: {e}")

        # Review time series (the slowest call)
        try:
            profile["review_timeseries"] = self.get_review_timeseries(
                appid, lookback_months=lookback_months, max_reviews=max_reviews
            )
        except SteamAPIError as e:
            profile["fetch_errors"].append(f"review_timeseries: {e}")

        # Current CCU
        try:
            profile["current_ccu"] = self.get_current_ccu(appid)
        except SteamAPIError as e:
            profile["fetch_errors"].append(f"current_ccu: {e}")

        # SteamCharts (optional, non-fatal)
        if include_steamcharts:
            profile["steamcharts_history"] = self.get_steamcharts_history(appid)

        return profile


# ---------------------------------------------------------------------------
# Utility: parse common fields from app_details
# ---------------------------------------------------------------------------

def parse_app_details(details: dict) -> dict:
    """
    Extract the most useful fields from a raw app_details response
    into a flat, typed dict for downstream processing.

    Source label: [observed] for factual fields, [derived] for computed ones.
    """
    price_block = details.get("price_overview", {})
    release = details.get("release_date", {})

    # Parse release date string (Steam returns e.g. "14 Feb, 2022")
    release_date_str = release.get("date", "")
    release_date = _parse_steam_date(release_date_str)

    # Count supported languages (HTML string — count commas + 1)
    lang_html = details.get("supported_languages", "")
    lang_count = _count_languages(lang_html)

    # DLC count
    dlc_list = details.get("dlc", []) or []

    # Metacritic
    meta = details.get("metacritic", {}) or {}
    metacritic_score = meta.get("score", None)

    # Categories → derive is_multiplayer flag
    categories = details.get("categories", []) or []
    cat_ids = {c.get("id") for c in categories}
    is_multiplayer = bool(cat_ids & {1, 9, 27, 36, 37, 49})  # MP-related Steam category IDs

    # Genres
    genres = [g.get("description", "") for g in (details.get("genres", []) or [])]

    # Current price in USD (cents → dollars)
    current_price_usd = None
    if price_block:
        final_cents = price_block.get("final", 0)
        current_price_usd = final_cents / 100 if final_cents else None

    # Age in months from release to today
    age_months = None
    if release_date:
        delta = datetime.now(timezone.utc) - release_date.replace(tzinfo=timezone.utc)
        age_months = max(delta.days / 30.44, 0.1)

    return {
        "appid":           details.get("steam_appid"),
        "name":            details.get("name", "Unknown"),
        "is_free":         details.get("is_free", False),
        "current_price_usd": current_price_usd,
        "genres":          genres,
        "primary_genre":   genres[0] if genres else "Other",
        "release_date":    release_date,
        "age_months":      age_months,
        "developers":      details.get("developers", []),
        "publishers":      details.get("publishers", []),
        "metacritic_score": metacritic_score,
        "language_count":  lang_count,
        "dlc_count":       len(dlc_list),
        "is_multiplayer":  is_multiplayer,
        "categories":      [c.get("description", "") for c in categories],
        "achievements_total": (details.get("achievements", {}) or {}).get("total", 0),
        "platforms":       details.get("platforms", {}),
        # Source labels for UI display
        "_source": {
            "current_price_usd": "observed",
            "release_date":      "observed",
            "metacritic_score":  "observed",
            "language_count":    "derived",
            "dlc_count":         "observed",
            "is_multiplayer":    "derived",
        },
    }


def parse_review_summary(summary: dict) -> dict:
    """
    Extract clean fields from a review summary dict.
    Source: [observed]
    """
    total = summary.get("total_reviews", 0)
    positive = summary.get("total_positive", 0)
    sentiment_ratio = positive / total if total > 0 else 0.0
    return {
        "total_reviews":   total,
        "total_positive":  positive,
        "total_negative":  summary.get("total_negative", 0),
        "sentiment_ratio": round(sentiment_ratio, 4),
        "sentiment_label": summary.get("review_score_desc", ""),
        "_source": "observed",
    }


def build_monthly_review_histogram(
    review_timeseries: list[dict],
    lookback_months: int = 24,
) -> list[dict]:
    """
    Convert a list of {timestamp, voted_up} review events into a monthly histogram.

    Returns list of dicts ordered oldest-first:
        [{"year_month": "YYYY-MM", "review_count": N, "positive_count": N}]

    Source: [derived] — computed from observed review timestamps.
    """
    from collections import defaultdict

    monthly: dict = defaultdict(lambda: {"review_count": 0, "positive_count": 0})

    for r in review_timeseries:
        dt = datetime.fromtimestamp(r["timestamp"], tz=timezone.utc)
        key = dt.strftime("%Y-%m")
        monthly[key]["review_count"] += 1
        if r.get("voted_up", True):
            monthly[key]["positive_count"] += 1

    # Build complete month range (fill zeros for months with no reviews)
    now = datetime.now(timezone.utc)
    result = []
    for i in range(lookback_months - 1, -1, -1):
        dt = now - timedelta(days=i * 30.44)
        key = dt.strftime("%Y-%m")
        bucket = monthly.get(key, {"review_count": 0, "positive_count": 0})
        result.append({
            "year_month":     key,
            "review_count":   bucket["review_count"],
            "positive_count": bucket["positive_count"],
        })

    return result


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _parse_steam_date(date_str: str):
    """Try to parse Steam's date string formats into a datetime. Returns None on failure."""
    formats = ["%d %b, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B, %Y"]
    for fmt in formats:
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None


def _count_languages(lang_html: str) -> int:
    """Count supported languages from Steam's HTML string (comma-separated)."""
    if not lang_html:
        return 0
    # Strip HTML tags
    import re
    clean = re.sub(r"<[^>]+>", "", lang_html)
    return len([x for x in clean.split(",") if x.strip()])
