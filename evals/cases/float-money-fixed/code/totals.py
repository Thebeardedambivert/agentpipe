from decimal import Decimal


def totals_match(prices, expected):
    """Report whether the basket adds up to the expected amount."""
    total = sum(Decimal(str(p)) for p in prices)
    return total == Decimal(str(expected))
