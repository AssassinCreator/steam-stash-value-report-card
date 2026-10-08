"""HTTP with manners: an honest User-Agent, per-host pacing, backoff with jitter and Retry-After.

Steam answers 429 quickly, especially to datacenter IPs. We never hammer: each host has a
minimum gap between requests, and a 429 either waits (if the wait is short) or raises
RateLimited so the caller can fall back to cache or another source and say so.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

from stashcard import USER_AGENT


class FetchError(Exception):
    """A request failed in a way the user should hear about."""


class OfflineError(FetchError):
    pass


class RateLimited(FetchError):
    def __init__(self, service: str, retry_after: float | None = None):
        self.service = service
        self.retry_after = retry_after
        super().__init__(rate_limit_message(service, retry_after))


class PrivateError(FetchError):
    pass


class NotFoundError(FetchError):
    pass


def rate_limit_message(service: str, retry_after: float | None) -> str:
    wait = "a few minutes" if not retry_after else f"~{max(1, round(retry_after / 60))} min"
    return (
        f"{service} is rate-limiting this connection (HTTP 429). Retry in {wait}, "
        "use cached data (--offline), or try `stashcard demo`."
    )


@dataclass
class HostPolicy:
    min_gap: float  # seconds between requests to this host
    service: str


POLICIES: dict[str, HostPolicy] = {
    "steamcommunity.com": HostPolicy(3.1, "Steam"),  # market: about 20 requests/min per IP
    "api.skinport.com": HostPolicy(1.0, "Skinport"),  # about 8 requests / 5 min; we batch whole apps
    "store.steampowered.com": HostPolicy(1.6, "Steam Store"),
    "api.steampowered.com": HostPolicy(0.4, "Steam Web API"),
    "api.waxpeer.com": HostPolicy(2.0, "Waxpeer"),
    "market.csgo.com": HostPolicy(2.0, "Market.CSGO"),
    "market.dota2.net": HostPolicy(2.0, "Market.CSGO"),
    "tf2.tm": HostPolicy(2.0, "Market.CSGO"),
    "rust.tm": HostPolicy(2.0, "Market.CSGO"),
    "lis-skins.com": HostPolicy(2.0, "Lis-skins"),
    "csfloat.com": HostPolicy(2.5, "CSFloat"),
    "open.er-api.com": HostPolicy(1.0, "open.er-api.com"),
}


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError):
            return None


class Net:
    def __init__(
        self,
        *,
        offline: bool = False,
        max_wait: float = 20.0,
        retries: int = 2,
        timeout: float = 20.0,
        on_wait: Callable[[str, float], None] | None = None,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.offline = offline
        self.max_wait = max_wait
        self.retries = retries
        self.on_wait = on_wait
        self.sleep = sleep
        self._last: dict[str, float] = {}
        self._blocked: dict[str, float] = {}  # host -> monotonic time until which we stop asking
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "br, gzip, deflate"},
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def is_blocked(self, host: str) -> bool:
        return self._blocked.get(host, 0) > time.monotonic()

    def _pace(self, host: str) -> None:
        policy = POLICIES.get(host)
        if not policy:
            return
        gap = time.monotonic() - self._last.get(host, 0.0)
        if gap < policy.min_gap:
            self.sleep(policy.min_gap - gap)
        self._last[host] = time.monotonic()

    def get(
        self,
        url: str,
        *,
        params: dict | None = None,
        headers: dict | None = None,
        cookies: dict | None = None,
    ) -> httpx.Response:
        if self.offline:
            raise OfflineError("Offline mode: not making network requests.")
        host = urlparse(url).hostname or ""
        service = POLICIES.get(host, HostPolicy(0, host)).service
        if self.is_blocked(host):
            left = self._blocked[host] - time.monotonic()
            raise RateLimited(service, left)
        attempt = 0
        while True:
            self._pace(host)
            try:
                resp = self.client.get(url, params=params, headers=headers, cookies=cookies)
            except httpx.TransportError as exc:
                if attempt < self.retries:
                    attempt += 1
                    self.sleep(1.5 * attempt + random.random())
                    continue
                raise FetchError(f"Could not reach {host}: {exc.__class__.__name__}. Check your connection.") from exc
            if resp.status_code == 429 or resp.status_code >= 500:
                ra = _retry_after(resp)
                backoff = ra if ra is not None else (2.0 * 2**attempt + random.uniform(0, 1.5))
                if attempt < self.retries and backoff <= self.max_wait:
                    attempt += 1
                    if self.on_wait:
                        self.on_wait(service, backoff)
                    self.sleep(backoff)
                    continue
                if resp.status_code == 429:
                    # Remember the block so the rest of this run does not keep knocking.
                    self._blocked[host] = time.monotonic() + (ra or 120.0)
                    raise RateLimited(service, ra)
                raise FetchError(f"{service} returned HTTP {resp.status_code}. Try again in a minute.")
            return resp
