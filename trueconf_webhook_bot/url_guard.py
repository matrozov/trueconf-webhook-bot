"""SSRF protection for externally supplied URLs.

The TrueConf bot downloads external URLs via `URLInputFile`, which performs a
HEAD and a GET request. To prevent an attacker with a valid token from forcing
the server to reach local services (IMDS, databases, admin panels), every URL
is validated here first.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

# Only network schemes are allowed. `file://`, `ftp://`, `gopher://`, ... are blocked.
_ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

# Hostname traps that cannot be caught by the IP blocklist but have well-known
# meaning in common infrastructure (cloud metadata services and similar).
_HOSTNAME_BLOCKLIST: frozenset[str] = frozenset({
    "localhost",
    "metadata.google.internal",
    "metadata.goog",
})


class InvalidAttachmentUrl(ValueError):
    """The URL failed the safety check."""


def validate_public_url(url: str, *, resolve_dns: bool = True) -> str:
    """Return the URL if it is safe for the server to fetch.

    Rules:
    - only `http(s)` — no `file`, `ftp`, or arbitrary schemes;
    - IP literals in the host are checked directly: private, loopback,
      link-local, multicast and reserved ranges are rejected;
    - hostnames are additionally resolved to IPs (when `resolve_dns` is True),
      and every resulting address is checked against the same blocklist. This
      mitigates DNS rebinding and public names pointing at the internal network.

    Args:
        url: raw URL string.
        resolve_dns: whether to resolve the hostname for the extra check.
                     Disable only in unit tests.

    Returns: the original `url` string if all checks passed.

    Raises: `InvalidAttachmentUrl` with a human-readable reason.
    """
    if not isinstance(url, str) or not url:
        raise InvalidAttachmentUrl("url is empty or not a string")

    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise InvalidAttachmentUrl(f"invalid url: {exc}") from exc

    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise InvalidAttachmentUrl(f"scheme {scheme!r} is not allowed")

    host = (parsed.hostname or "").lower()
    if not host:
        raise InvalidAttachmentUrl("url has no host")
    if host in _HOSTNAME_BLOCKLIST:
        raise InvalidAttachmentUrl(f"host {host!r} is blocked")

    # First, try to treat the host as an IP literal (IPv4 or IPv6).
    literal = _as_ip(host)
    if literal is not None:
        _reject_unsafe_ip(literal)
        return url

    if resolve_dns:
        try:
            addrinfos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise InvalidAttachmentUrl(f"dns: {exc}") from exc
        for family, _type, _proto, _canon, sockaddr in addrinfos:
            address_string = sockaddr[0]
            if family == socket.AF_INET6:
                # Drop the zone id if present: `fe80::1%eth0` -> `fe80::1`
                address_string = address_string.split("%", 1)[0]
            addr = _as_ip(address_string)
            if addr is None:
                continue
            _reject_unsafe_ip(addr)

    return url


def _as_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _reject_unsafe_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> None:
    if (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    ):
        raise InvalidAttachmentUrl(f"address {addr} is not reachable (private/reserved range)")
