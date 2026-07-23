def same_account(a, b):
    """Decide whether two typed email addresses are the same account."""
    return a.strip().casefold() == b.strip().casefold()
