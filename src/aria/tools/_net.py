"""
aria/tools/_net.py — shared outbound-network safety (SSRF guard).

Agent-driven fetches (web_fetch, browser) can be pointed at internal addresses
by a channel user or by prompt-injected content. This blocks requests to
private / loopback / link-local / reserved ranges — including the cloud metadata
endpoint 169.254.169.254 — and non-http(s) schemes, validating at every redirect
hop.

Underscore-prefixed so the tool auto-loader skips it (it's a helper, not a tool).
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urljoin, urlparse

_ALLOWED_SCHEMES = {"http", "https"}


class BlockedURL(ValueError):
    """Raised when a URL targets a non-public / disallowed address."""


def _ip_is_blocked(ip: str, *, allow_loopback: bool, allow_private: bool) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True  # unparseable → block
    # Always blocked — link-local covers the cloud metadata endpoint
    # (169.254.169.254), the highest-impact SSRF target.
    if addr.is_link_local or addr.is_reserved or addr.is_multicast or addr.is_unspecified:
        return True
    # IPv4-mapped IPv6 (::ffff:127.0.0.1) — judge the embedded IPv4 address.
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return _ip_is_blocked(str(mapped), allow_loopback=allow_loopback,
                              allow_private=allow_private)
    if addr.is_loopback:
        return not allow_loopback
    # Anything not globally routable — RFC1918, CGNAT 100.64/10, ULA fc00::/7,
    # benchmarking/documentation ranges… — is "private" for SSRF purposes.
    # (is_private alone misses 100.64/10.)
    if addr.is_private or not addr.is_global:
        return not (allow_private or _user_allowed(addr))
    return False


def _user_allowed(addr) -> bool:
    """ARIA_NET_ALLOW: comma-separated CIDRs the user explicitly trusts, e.g.
    100.64.0.0/10 (Tailscale) or 198.18.0.0/15 (fake-IP proxies such as
    Clash/sing-box, where EVERY hostname resolves into that range)."""
    raw = os.environ.get("ARIA_NET_ALLOW", "")
    for cidr in (c.strip() for c in raw.split(",")):
        if not cidr:
            continue
        try:
            if addr in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def validate_public_url(url: str, *, allow_loopback: bool = False,
                        allow_private: bool = False) -> None:
    """
    Raise BlockedURL unless `url` is an http(s) URL whose host resolves only to
    permitted addresses. Resolving + checking every returned address defeats
    hostnames that point at private IPs (e.g. a domain aliased to 127.0.0.1 or
    169.254.169.254). Link-local/reserved/multicast (incl. cloud metadata) are
    ALWAYS blocked; loopback/private can be opted in (e.g. browser for local dev).
    """
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise BlockedURL(f"scheme '{parsed.scheme}' not allowed (http/https only)")
    host = parsed.hostname
    if not host:
        raise BlockedURL("URL has no host")
    port = parsed.port or (443 if scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise BlockedURL(f"cannot resolve host '{host}': {exc}") from exc
    for info in infos:
        ip = str(info[4][0])
        if _ip_is_blocked(ip, allow_loopback=allow_loopback, allow_private=allow_private):
            raise BlockedURL(f"host '{host}' resolves to disallowed address {ip}")


def safe_get(url: str, *, max_redirects: int = 5, **client_kwargs):
    """
    httpx GET with the SSRF guard applied to the initial URL and to every
    redirect target (auto-redirects are disabled and followed manually so a
    public host can't 302 to an internal one). `client_kwargs` go to httpx.Client
    (timeout, headers, ...). Returns the final httpx.Response.
    """
    import httpx

    current = url
    redirects = 0
    with httpx.Client(follow_redirects=False, **client_kwargs) as client:
        while True:
            validate_public_url(current)
            resp = client.get(current)
            if resp.is_redirect and resp.headers.get("location") and redirects < max_redirects:
                current = urljoin(current, resp.headers["location"])
                redirects += 1
                continue
            return resp
