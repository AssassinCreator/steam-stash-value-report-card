"""Currency conversion for venues that only quote USD.

Rates come from open.er-api.com (free, no key, daily updates, includes RUB/UAH/KZT), are cached
for 12 hours, and every converted number carries the rate and its date. Demo and offline runs
use the dated fixture table.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

FX_URL = "https://open.er-api.com/v6/latest/USD"
FX_SOURCE = "open.er-api.com"


@dataclass(frozen=True)
class FxRate:
    base: str
    quote: str
    rate: float
    date: str  # YYYY-MM-DD of the provider's last update
    source: str

    def convert(self, minor: int | None) -> int | None:
        if minor is None:
            return None
        if self.base == self.quote:
            return minor
        return max(1, int(round(minor * self.rate))) if minor > 0 else 0

    @property
    def identity(self) -> bool:
        return self.base == self.quote

    @property
    def label(self) -> str:
        """'1 USD = 0.8972 EUR (open.er-api.com, 2026-10-29)'."""
        return f"1 {self.base} = {self.rate:.6g} {self.quote} ({self.source}, {self.date})"

    @property
    def short(self) -> str:
        return f"{self.base}→{self.quote} {self.rate:.6g} @{self.date}"

    def to_dict(self) -> dict:
        return {
            "base": self.base,
            "quote": self.quote,
            "rate": self.rate,
            "date": self.date,
            "source": self.source,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> FxRate | None:
        if not d:
            return None
        return cls(d["base"], d["quote"], float(d["rate"]), d["date"], d["source"])


@dataclass(frozen=True)
class FxTable:
    base: str
    rates: dict
    date: str
    source: str

    def rate(self, quote: str) -> FxRate | None:
        if quote == self.base:
            return FxRate(self.base, quote, 1.0, self.date, self.source)
        value = self.rates.get(quote)
        if not value:
            return None
        return FxRate(self.base, quote, float(value), self.date, self.source)


def parse_er_api(doc: dict) -> FxTable:
    """Parse open.er-api.com's /v6/latest/USD response."""
    if not isinstance(doc, dict) or doc.get("result") != "success" or not isinstance(doc.get("rates"), dict):
        raise ValueError("Unexpected FX response")
    date = ""
    if doc.get("time_last_update_unix"):
        date = datetime.fromtimestamp(int(doc["time_last_update_unix"]), tz=timezone.utc).strftime("%Y-%m-%d")
    elif doc.get("time_last_update_utc"):
        date = parsedate_to_datetime(doc["time_last_update_utc"]).strftime("%Y-%m-%d")
    return FxTable(doc.get("base_code", "USD"), {k: float(v) for k, v in doc["rates"].items()}, date, FX_SOURCE)
