"""Pin outbound sockets to IPv4.

Kite's order APIs compare the request's source address against the "allowed
IPs" configured for the app.  Windows prefers IPv6 (RFC 6724) and its
temporary privacy address rotates, so a live run egressed on a short-lived
IPv6 address while the console had the IPv4 whitelisted:

    IP (2401:4900:...) is not allowed to place orders for this app

Filtering ``getaddrinfo`` to ``AF_INET`` keeps egress on the whitelisted IPv4
without disabling IPv6 machine-wide, without admin rights, and without a
reboot.  Callers that explicitly ask for ``AF_INET6`` are left alone.
"""

from __future__ import annotations

import socket

_APPLIED = False


def ipv4_only_addrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """``socket.getaddrinfo`` restricted to IPv4 unless IPv6 is demanded."""
    if family in (0, socket.AF_UNSPEC):
        family = socket.AF_INET
    return ipv4_only_addrinfo.original(host, port, family, type, proto, flags)


def prefer_ipv4() -> bool:
    """Force unnamed-family lookups onto IPv4.

    Returns True when the patch was installed by this call, False when it was
    already active.  Safe to call from every entry point.
    """
    global _APPLIED
    if _APPLIED:
        return False
    ipv4_only_addrinfo.original = socket.getaddrinfo
    socket.getaddrinfo = ipv4_only_addrinfo
    _APPLIED = True
    return True


def reset() -> None:
    """Undo :func:`prefer_ipv4` (tests and long-lived embeds only)."""
    global _APPLIED
    if _APPLIED:
        socket.getaddrinfo = ipv4_only_addrinfo.original
        _APPLIED = False


def egress_ip(host: str = "104.16.34.50", port: int = 443) -> str:
    """Return the local address a TCP connect to ``host`` would use."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((host, port))
        return probe.getsockname()[0]
