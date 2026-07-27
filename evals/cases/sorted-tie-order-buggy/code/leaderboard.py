def rank(players):
    """Order (name, score) pairs highest score first."""
    return list(reversed(sorted(players, key=lambda p: p[1])))
