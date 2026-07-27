def chunk(items, size):
    """Split items into consecutive pieces of at most size items."""
    if size <= 0:
        raise ValueError(f"size must be positive, got {size}")
    return [items[i:i + size] for i in range(0, len(items), size)]
