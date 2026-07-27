def record_metric(store, row):
    """Persist one metric row. Never fails the caller."""
    try:
        store.write(row)
    except Exception:
        pass
