"""Client IPs are truncated to /24 (IPv4) or /48 (IPv6). Documentation ranges only."""

from __future__ import annotations

from ipaddress import IPv4Network, IPv6Network, ip_address

import pytest

from app.db.ip import ip_prefix


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("203.0.113.77", IPv4Network("203.0.113.0/24")),
        ("203.0.113.0", IPv4Network("203.0.113.0/24")),
        ("198.51.100.255", IPv4Network("198.51.100.0/24")),
        ("2001:db8:1234:5678:9abc::1", IPv6Network("2001:db8:1234::/48")),
        ("2001:db8::", IPv6Network("2001:db8::/48")),
        ("::ffff:203.0.113.77", IPv4Network("203.0.113.0/24")),  # IPv4-mapped
        ("fe80::1%eth0", IPv6Network("fe80::/48")),  # scope ID dropped
    ],
)
def test_ip_prefix(address: str, expected: IPv4Network | IPv6Network) -> None:
    assert ip_prefix(address) == expected


def test_accepts_address_objects() -> None:
    assert ip_prefix(ip_address("203.0.113.77")) == IPv4Network("203.0.113.0/24")


@pytest.mark.parametrize("bad", ["", "not-an-ip", "203.0.113.77/24", "203.0.113.256"])
def test_invalid_input_is_not_echoed(bad: str) -> None:
    with pytest.raises(ValueError) as info:
        ip_prefix(bad)
    assert str(info.value) == "not an IP address"
