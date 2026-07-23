_BANDS = {"low": (0, 10), "high": (0, 100)}


def clamp(value, band):
    """Limit value to the named band.

    An unknown band raises KeyError from the lookup below, naming the band. That
    is deliberate: re-raising it as a ValueError would add a line and say less.
    """
    lo, hi = _BANDS[band]
    return max(lo, min(value, hi))
