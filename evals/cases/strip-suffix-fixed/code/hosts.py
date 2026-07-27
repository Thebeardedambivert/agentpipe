def normalise_host(host):
    """Remove the trailing .com from a hostname."""
    return host.removesuffix(".com")
