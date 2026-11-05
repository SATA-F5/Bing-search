"""Async Bing web search client for the AstrBot plugin.

This module deliberately follows the same result shape as AstrBot's built-in
web-search tools (``SearchResult`` + JSON ``{"results": [...]}``), so the LLM
sees a familiar payload without requiring a search-engine API key.

Two data sources are supported:

* Bing's RSS result feed (smaller, stable, easy to parse).
* Bing's regular HTML result page (used as a fallback when RSS is unavailable).

All network access is asynchronous (``aiohttp``); no blocking ``requests`` calls
and no local files are used.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import html as html_lib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree

import aiohttp
from bs4 import BeautifulSoup

DEFAULT_ENDPOINT = "https://www.bing.com/search"
DEFAULT_MARKET = "zh-CN"

# A couple of common desktop user agents. Bing serves slightly different
# markup depending on the client, but both of these work for RSS and HTML.
USER_AGENTS = (
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
    ),
)

_FRESHNESS_FILTERS = {
    "day": 'ex1:"ez1"',
    "week": 'ex1:"ez2"',
    "month": 'ex1:"ez3"',
}

_SETLANG_BY_LANGUAGE = {
    "zh": "zh-hans",
    "en": "en-US",
    "ja": "ja-JP",
    "ko": "ko-KR",
    "fr": "fr-FR",
    "de": "de-DE",
    "es": "es-ES",
    "ru": "ru-RU",
}


class BingSearchError(Exception):
    """Raised when a Bing request cannot be completed."""


@dataclass
class SearchResult:
    """A single normalized search result (same shape as AstrBot's built-in)."""

    title: str
    url: str
    snippet: str
    favicon: str | None = None


def _config_get(config: Any, key: str, default: Any) -> Any:
    """Read a value from a dict-like AstrBot config object."""
    try:
        value = config.get(key, default)
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive config read
        return default
    return default if value is None else value


def _config_int(config: Any, key: str, default: int, low: int, high: int) -> int:
    try:
        value = int(_config_get(config, key, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _config_float(
    config: Any, key: str, default: float, low: float, high: float
) -> float:
    try:
        value = float(_config_get(config, key, default))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _config_str(config: Any, key: str, default: str = "") -> str:
    value = _config_get(config, key, default)
    return str(value).strip() if value is not None else default


def _market_params(
    market: str | None, fallback: str = DEFAULT_MARKET
) -> dict[str, str]:
    """Translate a market such as ``zh-CN`` into Bing query parameters."""
    market = (market or fallback or DEFAULT_MARKET).strip()
    parts = re.split(r"[-_]", market, maxsplit=1)
    language = parts[0].lower() if parts else ""
    country = parts[1].upper() if len(parts) > 1 else ""

    setlang = _SETLANG_BY_LANGUAGE.get(language, market)
    if language == "zh" and country.upper() in {"TW", "HK", "MO"}:
        setlang = "zh-hant"

    params = {"mkt": market, "setlang": setlang}
    if country:
        params["cc"] = country
    return params


def _strip_html(value: str | None) -> str:
    if not value:
        return ""
    text = BeautifulSoup(html_lib.unescape(value), "html.parser").get_text(" ")
    return " ".join(text.split())


def _decode_bing_redirect(url: str) -> str:
    """Decode ``bing.com/ck/a?...&u=a1<base64>`` tracking links."""
    if not url:
        return ""
    url = html_lib.unescape(url).strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith(("http://", "https://")):
        return url

    parsed = urlparse(url)
    if not parsed.netloc.lower().endswith("bing.com") or not parsed.path.startswith(
        "/ck/a"
    ):
        return url

    encoded = parse_qs(parsed.query).get("u", [""])[0]
    if not encoded:
        return url

    encoded = encoded.removeprefix("a1")
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        raw = base64.urlsafe_b64decode(padded)
    except (ValueError, binascii.Error):
        return url

    for encoding in ("utf-8", "gb18030", "latin-1"):
        try:
            candidate = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if candidate.startswith(("http://", "https://")):
            return candidate
    return url


def parse_bing_rss(xml_text: str) -> list[SearchResult]:
    """Parse Bing's RSS search feed."""
    if not xml_text or "<rss" not in xml_text[:1000].lower():
        return []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []

    channel = root.find("channel")
    if channel is None:
        return []

    results: list[SearchResult] = []
    for item in channel.findall("item"):
        title = _strip_html(item.findtext("title"))
        url = _decode_bing_redirect((item.findtext("link") or "").strip())
        snippet = _strip_html(item.findtext("description"))
        if title and url:
            results.append(SearchResult(title=title, url=url, snippet=snippet))
    return results


def parse_bing_html(html_text: str) -> list[SearchResult]:
    """Parse Bing's regular HTML search result page."""
    if not html_text or "b_algo" not in html_text:
        return []

    soup = BeautifulSoup(html_text, "html.parser")
    results: list[SearchResult] = []
    for block in soup.select("li.b_algo"):
        # Skip obvious advertisements while keeping normal organic results.
        classes = " ".join(block.get("class") or [])
        if "b_ad" in classes or block.select_one(".b_ad, .b_adlabel, [data-bm='ad']"):
            continue

        anchor = block.select_one("h2 a[href]")
        if anchor is None:
            anchor = block.select_one("a[href]")
        if anchor is None:
            continue

        title = " ".join(anchor.get_text(" ", strip=True).split())
        url = _decode_bing_redirect(anchor.get("href") or "")
        if not title or not url.startswith(("http://", "https://")):
            continue

        snippet = ""
        for selector in (".b_caption p", ".b_snippet", "p.b_lineclamp1", "p"):
            node = block.select_one(selector)
            if node:
                snippet = " ".join(node.get_text(" ", strip=True).split())
                if snippet:
                    break

        results.append(SearchResult(title=title, url=url, snippet=snippet))
    return results


def build_search_payload(results: list[SearchResult]) -> str:
    """Serialize results exactly like AstrBot's built-in web-search tools."""
    ref_uuid = str(uuid.uuid4())[:4]
    payload = []
    for index, result in enumerate(results, 1):
        payload.append(
            {
                "title": result.title,
                "url": result.url,
                "snippet": result.snippet,
                "index": f"{ref_uuid}.{index}",
            }
        )
    return json.dumps({"results": payload}, ensure_ascii=False)


class BingSearchClient:
    """Small async Bing scraper with RSS-first and HTML fallback modes."""

    def __init__(self, config: Any | None = None) -> None:
        self.config = config if config is not None else {}

        self.endpoint = _config_str(self.config, "search_endpoint", DEFAULT_ENDPOINT)
        if not self.endpoint.startswith(("http://", "https://")):
            self.endpoint = DEFAULT_ENDPOINT

        self.market = (
            _config_str(self.config, "market", DEFAULT_MARKET) or DEFAULT_MARKET
        )
        self.safe_search = _config_str(self.config, "safe_search", "moderate").lower()
        if self.safe_search not in {"off", "moderate", "strict"}:
            self.safe_search = "moderate"

        self.fetch_mode = _config_str(self.config, "fetch_mode", "auto").lower()
        if self.fetch_mode not in {"auto", "rss", "html"}:
            self.fetch_mode = "auto"

        self.default_count = _config_int(self.config, "default_count", 8, 1, 10)
        self.timeout = _config_float(self.config, "timeout", 15.0, 3.0, 60.0)
        self.max_retries = _config_int(self.config, "max_retries", 2, 1, 4)
        self.proxy = _config_str(self.config, "proxy", "")
        self.cookie = _config_str(self.config, "cookie", "")
        self.user_agent = _config_str(self.config, "user_agent", "")

        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._session_lock:
            if self._session is None or self._session.closed:
                timeout = aiohttp.ClientTimeout(
                    total=self.timeout,
                    connect=min(10.0, self.timeout),
                )
                self._session = aiohttp.ClientSession(
                    timeout=timeout,
                    trust_env=True,
                    connector=aiohttp.TCPConnector(limit=8, ttl_dns_cache=300),
                )
        return self._session

    def _headers(
        self,
        accept: str,
        attempt: int,
        market: str | None,
        referer: str | None = None,
    ) -> dict[str, str]:
        params = _market_params(market, self.market)
        language = params.get("setlang", "zh-hans")
        headers = {
            "User-Agent": self.user_agent or USER_AGENTS[attempt % len(USER_AGENTS)],
            "Accept": accept,
            "Accept-Language": f"{language},{language.split('-')[0]};q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": referer or "https://www.bing.com/",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _params(
        self,
        query: str,
        *,
        count: int,
        market: str | None,
        freshness: str | None,
        rss: bool,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "q": query,
            "form": "QBLH",
            "count": min(10, max(1, count)),
        }
        params.update(_market_params(market, self.market))

        if self.safe_search in {"off", "strict"}:
            params["adlt"] = self.safe_search
        if freshness:
            freshness = freshness.lower().strip()
            if freshness in _FRESHNESS_FILTERS:
                params["filters"] = _FRESHNESS_FILTERS[freshness]
        if rss:
            params["format"] = "rss"
        return params

    async def _request(
        self,
        params: dict[str, Any] | None = None,
        *,
        url: str | None = None,
        accept: str,
        market: str | None,
        referer: str | None = None,
    ) -> str:
        session = await self._get_session()
        last_error: Exception | None = None
        target = url or self.endpoint

        for attempt in range(self.max_retries):
            kwargs: dict[str, Any] = {
                "headers": self._headers(accept, attempt, market, referer),
                "allow_redirects": True,
            }
            if params is not None:
                kwargs["params"] = params
            if self.proxy:
                kwargs["proxy"] = self.proxy

            try:
                async with session.get(target, **kwargs) as response:
                    body = await response.text(errors="replace")
                    if response.status == 429 or 500 <= response.status < 600:
                        last_error = BingSearchError(
                            f"Bing returned HTTP {response.status}"
                        )
                        if attempt + 1 < self.max_retries:
                            await asyncio.sleep(0.35 * (attempt + 1))
                            continue
                        raise last_error
                    if response.status >= 400:
                        raise BingSearchError(f"Bing returned HTTP {response.status}")
                    return body
            except BingSearchError:
                raise
            except (  # noqa: UP041 - keep Python 3.10 compatibility
                TimeoutError,
                asyncio.TimeoutError,
                aiohttp.ClientError,
            ) as exc:
                last_error = BingSearchError(f"Bing request failed: {exc}")
                if attempt + 1 < self.max_retries:
                    await asyncio.sleep(0.35 * (attempt + 1))
                    continue

        if last_error is not None:
            raise last_error
        raise BingSearchError("Bing request failed")

    async def _search_rss(
        self,
        query: str,
        *,
        count: int,
        market: str | None,
        freshness: str | None,
    ) -> list[SearchResult]:
        # Bing's public RSS feed exposes one page (about 10 items).
        body = await self._request(
            self._params(
                query,
                count=count,
                market=market,
                freshness=freshness,
                rss=True,
            ),
            accept="application/rss+xml, application/xml;q=0.9, text/xml;q=0.8, */*;q=0.5",
            market=market,
        )
        return parse_bing_rss(body)[:count]

    async def _search_html(
        self,
        query: str,
        *,
        count: int,
        market: str | None,
        freshness: str | None,
    ) -> list[SearchResult]:
        body = await self._request(
            self._params(
                query,
                count=count,
                market=market,
                freshness=freshness,
                rss=False,
            ),
            accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            market=market,
        )
        return parse_bing_html(body)[:count]

    async def search(
        self,
        query: str,
        *,
        count: int | None = None,
        market: str | None = None,
        freshness: str | None = None,
    ) -> list[SearchResult]:
        """Search Bing and return normalized results.

        Raises:
            BingSearchError: If every enabled data source failed.
        """
        query = " ".join(str(query or "").split())
        if not query:
            return []

        try:
            count = int(count) if count is not None else self.default_count
        except (TypeError, ValueError):
            count = self.default_count
        count = max(1, min(10, count))

        errors: list[Exception] = []
        succeeded = False

        async def run_mode(rss: bool) -> list[SearchResult]:
            nonlocal succeeded
            if rss:
                result = await self._search_rss(
                    query, count=count, market=market, freshness=freshness
                )
            else:
                result = await self._search_html(
                    query, count=count, market=market, freshness=freshness
                )
            succeeded = True
            return result

        if self.fetch_mode == "rss":
            modes = [True]
        elif self.fetch_mode == "html":
            modes = [False]
        else:
            modes = [True, False]

        for rss in modes:
            try:
                results = await run_mode(rss)
            except Exception as exc:  # noqa: BLE001 - try the next data source
                errors.append(exc)
                continue
            if results:
                return results[:count]

        if not succeeded and errors:
            raise errors[-1]
        return []

    async def close(self) -> None:
        """Close the underlying aiohttp session (called on plugin unload)."""
        session = self._session
        self._session = None
        if session is not None and not session.closed:
            await session.close()
