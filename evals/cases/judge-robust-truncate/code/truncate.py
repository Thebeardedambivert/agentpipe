def truncate(text, length):
    """Shorten text to at most length characters."""
    if length < 0:
        raise ValueError(f"length must not be negative, got {length}")
    return text[:length]
