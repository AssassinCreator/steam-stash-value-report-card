import pytest

from stashcard.money import Wallet, buyer_pays, fee_breakdown, format_money, parse_price, seller_receives, to_minor


@pytest.mark.parametrize(
    "text,minor",
    [
        ("$31.90", 3190),
        ("31,90€", 3190),
        ("1 234,56 p\u0443\u0431.", 123456),  # Steam's RUB format, written with escapes
        ("1 234,56₽", 123456),
        ("R$ 12,34", 1234),
        ("CHF 1'234.50", 123450),
        ("12,--€", 1200),
        ("Rp 15 000", 1500000),
        ("15.000₫", 1500000),
        ("₩ 1,500", 150000),
        ("$1,495.00", 149500),
        ("1,147.6", 114760),
        ("0,03€", 3),
        ("¥ 7.12", 712),
        ("kr 9,50", 950),
        ("1.234,56 zł", 123456),
        ("0.147", 14),
    ],
)
def test_parse_price_locales(text, minor):
    assert parse_price(text) == minor


@pytest.mark.parametrize("text", [None, "", "--", "Free", "€"])
def test_parse_price_nothing(text):
    assert parse_price(text) is None


def test_to_minor_rounds_half_up():
    assert to_minor(31.9) == 3190
    assert to_minor(0.005) == 1
    assert to_minor(None) is None


@pytest.mark.parametrize(
    "buyer,seller",
    [
        (3, 1),  # the market minimum: buyer pays 0.03, seller gets 0.01
        (4, 2),
        (10, 8),
        (20, 18),
        (100, 88),
        (115, 100),  # "you receive 1.00" -> "buyer pays 1.15"
        (3190, 2775),
        (149500, 130000),
    ],
)
def test_seller_receives_known_values(buyer, seller):
    assert seller_receives(buyer) == seller


def test_seller_receives_is_exact_inverse_of_buyer_pays():
    # Valve's GetItemPriceFromTotal must invert GetTotalWithFees for every listable amount.
    for received in range(1, 20001):
        assert seller_receives(buyer_pays(received)) == received


def test_fee_breakdown_adds_up():
    for price in (3, 57, 1000, 3190, 149500):
        f = fee_breakdown(price)
        assert f["seller_receives"] + f["steam_fee"] + f["publisher_fee"] == price


def test_unreachable_total_rounds_down_like_steam():
    # No "you receive" amount produces a 9.99 total; Steam settles on the amount below it.
    assert seller_receives(999) == 869
    assert buyer_pays(869) == 998
    assert buyer_pays(870) == 1000


def test_publisher_fee_zero():
    w = Wallet(publisher_fee=0.0)
    assert buyer_pays(100, w) == 105
    assert seller_receives(105, w) == 100


def test_format_money():
    assert format_money(149500, "USD") == "$1,495.00"
    assert format_money(3190, "EUR") == "€31.90"
    assert format_money(123456, "RUB") == "1,234.56 ₽"
    assert format_money(1500000, "IDR") == "Rp 15,000"
    assert format_money(-4137, "USD", sign=True) == "-$41.37"
    assert format_money(32768, "USD", sign=True) == "+$327.68"
    assert format_money(1240000, "USD", compact=True) == "$12.4k"
    assert format_money(None, "USD") == "n/a"
