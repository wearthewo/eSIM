"""Truncate client IPs before they reach the audit log.

Only the network prefix is ever stored: IPv4 /24 or IPv6 /48. The database
enforces the same rule (``audit_log.ip_prefix`` is a CIDR with a CHECK on the
mask length), so a full address cannot be stored by mistake.
"""

from __future__ import annotations

import ipaddress
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network

IPV4_PREFIX_LEN = 24
IPV6_PREFIX_LEN = 48


def ip_prefix(address: str | IPv4Address | IPv6Address) -> IPv4Network | IPv6Network:
    """The /24 (IPv4) or /48 (IPv6) network containing ``address``.

    IPv4-mapped IPv6 addresses (``::ffff:a.b.c.d``) are treated as IPv4. Raises
    ``ValueError`` without echoing the input if it is not an IP address.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        raise ValueError("not an IP address") from None
    if isinstance(ip, IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if isinstance(ip, IPv4Address):
        host_bits = 32 - IPV4_PREFIX_LEN
        return IPv4Network((int(ip) >> host_bits << host_bits, IPV4_PREFIX_LEN))
    host_bits = 128 - IPV6_PREFIX_LEN
    return IPv6Network((int(ip) >> host_bits << host_bits, IPV6_PREFIX_LEN))
