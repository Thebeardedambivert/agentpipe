def rank(players):
    """Order (name, score) pairs highest score first."""
    return sorted(players, key=lambda p: p[1], reverse=True)
