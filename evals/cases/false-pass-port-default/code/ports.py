DEFAULT_PORT = 8080


def parse_port(raw):
    """Parse a TCP port from a configuration string."""
    port = int(raw)
    if 1 <= port <= 65535:
        return port
    return DEFAULT_PORT
