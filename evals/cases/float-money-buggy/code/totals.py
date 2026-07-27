def totals_match(prices, expected):
    """Report whether the basket adds up to the expected amount."""
    return sum(prices) == expected
