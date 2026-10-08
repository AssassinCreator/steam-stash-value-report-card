"""Currencies, locale-tolerant price parsing, formatting and Valve's seller-fee maths.

All amounts inside Stashcard are integers in minor units ("cents"), exactly like Steam's
own wallet code. Steam uses two implied decimals for every currency, including IDR, VND
and KRW, so one rule covers them all.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Currency:
    code: str
    symbol: str
    prefix: bool = True  # symbol before the number
    steam_id: int | None = None  # Steam Community Market currency id
    skinport: bool = False  # supported by the Skinport public API
    decimals: int = 2  # decimals shown on cards (storage is always minor units)

    def label(self) -> str:
        where = []
        if self.steam_id:
            where.append("Steam")
        if self.skinport:
            where.append("Skinport")
        return f"{self.code}  {self.symbol.strip()}  ({' + '.join(where)})"


def _c(code, symbol, prefix=True, steam_id=None, skinport=False, decimals=2):
    return Currency(code, symbol, prefix, steam_id, skinport, decimals)


CURRENCIES: dict[str, Currency] = {
    c.code: c
    for c in [
        _c("USD", "$", True, 1, True),
        _c("EUR", "€", True, 3, True),
        _c("GBP", "£", True, 2, True),
        _c("CHF", "CHF ", True, 4, True),
        _c("RUB", " ₽", False, 5, True),
        _c("PLN", " zł", False, 6, True),
        _c("BRL", "R$ ", True, 7, True),
        _c("NOK", " kr", False, 9, True),
        _c("IDR", "Rp ", True, 10, False, 0),
        _c("MYR", "RM ", True, 11),
        _c("PHP", "₱", True, 12),
        _c("SGD", "S$", True, 13),
        _c("THB", "฿", True, 14),
        _c("VND", " ₫", False, 15, False, 0),
        _c("KRW", "₩", True, 16, False, 0),
        _c("TRY", "₺", True, 17, True),
        _c("UAH", " ₴", False, 18),
        _c("MXN", "Mex$ ", True, 19),
        _c("CAD", "CA$ ", True, 20, True),
        _c("AUD", "A$ ", True, 21, True),
        _c("NZD", "NZ$ ", True, 22),
        _c("CNY", "¥", True, 23, True),
        _c("INR", "₹", True, 24),
        _c("KZT", " ₸", False, 37),
        _c("SEK", " kr", False, None, True),
        _c("DKK", " kr.", False, None, True),
        _c("CZK", " Kč", False, None, True),
    ]
}


def currency(code: str) -> Currency:
    try:
        return CURRENCIES[code.upper()]
    except KeyError:
        known = ", ".join(sorted(CURRENCIES))
        raise ValueError(f"Unknown currency {code!r}. Supported: {known}") from None


def format_money(minor: int | None, code: str, *, compact: bool = False, sign: bool = False) -> str:
    """Format minor units for display: 149500 USD -> "$1,495.00".

    ``compact`` shortens big values ("$12.4k") for tight spaces such as grid tiles.
    ``sign`` forces a leading + or - (used for deltas).
    """
    if minor is None:
        return "n/a"
    cur = CURRENCIES.get(code.upper()) or Currency(code.upper(), f" {code.upper()}", False)
    neg = minor < 0
    value = abs(minor) / 100
    if compact and value >= 10_000:
        for div, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
            if value >= div:
                num = value / div
                body = f"{num:.1f}".rstrip("0").rstrip(".") + suffix if num < 100 else f"{num:.0f}{suffix}"
                break
    else:
        decimals = cur.decimals
        if compact and value >= 1000:
            decimals = 0
        body = f"{value:,.{decimals}f}"
    text = f"{cur.symbol}{body}" if cur.prefix else f"{body}{cur.symbol}"
    if neg:
        text = "-" + text
    elif sign:
        text = "+" + text
    return text


def to_minor(value: float | int | str | None) -> int | None:
    """Major units (as Skinport returns them, e.g. 31.9) to minor units, rounded half up."""
    if value is None or value == "":
        return None
    try:
        return int(math.floor(float(value) * 100 + 0.5))
    except (TypeError, ValueError):
        return None


_KEEP = re.compile(r"[^0-9.,\-]")


def parse_price(text: str | None) -> int | None:
    """Parse a Steam locale-formatted price string into minor units.

    Handles "$31.90", "31,90€", "1 234,56 <RUB symbol>.", "R$ 12,34", "CHF 1'234.50",
    "12,--€", "Rp 15 000", "15.000₫" and "₩ 1,500". Follows the same rule as
    Steam's GetPriceValueAsInt: the last separator is the decimal mark unless it is
    followed by exactly three digits, in which case every separator groups thousands.
    """
    if not text:
        return None
    s = _KEEP.sub("", str(text))
    s = s.replace(",", ".")
    s = s.replace(".--", ".00").replace("--", "")
    s = s.strip(".-")
    if not s or not any(ch.isdigit() for ch in s):
        return None
    if "." in s:
        parts = s.split(".")
        last = parts[-1]
        if last.isdigit() and len(last) == 3 and parts[-2] != "0":
            s = "".join(parts)
        else:
            s = "".join(parts[:-1]) + "." + last
    s = s.replace("-", "")
    try:
        amount = float(s) * 100
    except ValueError:
        return None
    return max(0, int(math.floor(amount + 0.000001)))


# --------------------------------------------------------------------------------------
# Valve seller fees
# --------------------------------------------------------------------------------------
# A straight port of ToValidMarketPrice / CalculateFee / GetTotalWithFees /
# GetItemPriceFromTotal from Steam's public economy_common.js (read on 2026-10-29).
# Floating point operations keep Valve's order so results match the market UI to the cent.

STEAM_FEE = 0.05
DEFAULT_PUBLISHER_FEE = 0.10


@dataclass(frozen=True)
class Wallet:
    steam_fee: float = STEAM_FEE
    publisher_fee: float = DEFAULT_PUBLISHER_FEE
    market_minimum: int = 1
    currency_increment: int = 1


DEFAULT_WALLET = Wallet()


def _to_valid_market_price(price: int, w: Wallet) -> int:
    floor_, inc = w.market_minimum, w.currency_increment
    if price <= floor_:
        return floor_
    if price <= inc:
        return inc
    if inc > 1:
        amount = price / inc
        sign = -1 if amount < 0 else 1
        return int((sign * math.floor(abs(amount) + 0.5)) * inc)
    return price


def _calculate_fee(base: int, pct: float, w: Wallet) -> int:
    if pct > 0:
        return _to_valid_market_price(int(math.floor(base * pct)), w)
    return 0


def buyer_pays(seller_receives: int, wallet: Wallet = DEFAULT_WALLET) -> int:
    """Price the buyer pays when the seller wants to receive ``seller_receives``."""
    base = _to_valid_market_price(seller_receives, wallet)
    return (
        base
        + _calculate_fee(seller_receives, wallet.publisher_fee, wallet)
        + _calculate_fee(seller_receives, wallet.steam_fee, wallet)
    )


def seller_receives(buyer_price: int, wallet: Wallet = DEFAULT_WALLET) -> int:
    """What the seller receives when an item sells for ``buyer_price`` (Valve's exact rounding)."""
    if buyer_price <= 0:
        return 0
    ppct, spct = wallet.publisher_fee, wallet.steam_fee
    inc, floor_ = wallet.currency_increment, wallet.market_minimum
    initial_guess = math.floor(buyer_price / (1.0 + ppct + spct))
    max_base = buyer_price - (2 * floor_)
    base = _to_valid_market_price(min(initial_guess, max_base), wallet)
    for _ in range(3):
        calculated = buyer_pays(base, wallet)
        if calculated == buyer_price:
            return base
        if calculated < buyer_price:
            base += inc
        else:
            base -= inc
            break
    return max(floor_, base)


def fee_breakdown(buyer_price: int, wallet: Wallet = DEFAULT_WALLET) -> dict[str, int]:
    net = seller_receives(buyer_price, wallet)
    steam = _calculate_fee(net, wallet.steam_fee, wallet)
    publisher = _calculate_fee(net, wallet.publisher_fee, wallet)
    return {"buyer_pays": buyer_price, "seller_receives": net, "steam_fee": steam, "publisher_fee": publisher}
