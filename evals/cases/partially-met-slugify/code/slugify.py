def slugify(title):
    """Turn a title into a URL slug."""
    return title.strip().lower().replace(" ", "-")
