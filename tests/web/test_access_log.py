"""uvicorn's access log holds only the client's IP prefix (as the audit log: /24 or /48),
never the full address or port, and never an invite token.

The unit tests build records exactly as uvicorn 0.54 does; the last test runs a real
uvicorn server, so a change in uvicorn's log call cannot slip past. No database needed.
All addresses are from the documentation ranges (RFC 5737, RFC 3849) or loopback.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.request

import pytest
import uvicorn

from app.auth.tokens import new_token
from app.settings import load_settings
from app.web.auth import (
    ACCESS_LOGGER,
    TruncateClientAddress,
    client_prefix_text,
    install_log_redaction,
)
from app.web.main import create_app

ACCESS_FORMAT = '%s - "%s %s HTTP/%s" %d'  # uvicorn.protocols.http.*_impl
UNREACHABLE_DB = "postgresql://esim:synthetic-password@127.0.0.1:1/esim?connect_timeout=2"


def _record(client_addr: str, path: str = "/healthz") -> logging.LogRecord:
    return logging.LogRecord(
        ACCESS_LOGGER,
        logging.INFO,
        __file__,
        1,
        ACCESS_FORMAT,
        (client_addr, "GET", path, "1.1", 200),
        None,
    )


@pytest.mark.parametrize(
    ("client_addr", "prefix", "full"),
    [
        ("203.0.113.77:51234", "203.0.113.0/24", "203.0.113.77"),
        ("2001:db8:abcd:12:3456::9:443", "2001:db8:abcd::/48", "2001:db8:abcd:12"),
        ("::ffff:198.51.100.9:8080", "198.51.100.0/24", "198.51.100.9"),
        ("192.0.2.200", "192.0.2.0/24", "192.0.2.200"),  # no port
    ],
    ids=["ipv4", "ipv6", "ipv4-mapped", "no-port"],
)
def test_access_line_has_only_the_prefix(client_addr: str, prefix: str, full: str) -> None:
    record = _record(client_addr)
    assert TruncateClientAddress().filter(record)
    message = record.getMessage()
    assert message.split(" - ", 1)[0] == prefix  # no full address, no port
    assert full not in message
    assert message.endswith('"GET /healthz HTTP/1.1" 200')


@pytest.mark.parametrize("client_addr", ["", "unix-socket", "not:an:ip:x"])
def test_other_clients_become_a_dash(client_addr: str) -> None:
    record = _record(client_addr)
    TruncateClientAddress().filter(record)
    assert record.getMessage().startswith('- - "GET /healthz')


def test_other_messages_are_left_alone() -> None:
    record = logging.LogRecord(ACCESS_LOGGER, logging.INFO, __file__, 1, "%s ready", ("x",), None)
    TruncateClientAddress().filter(record)
    assert record.getMessage() == "x ready"


def test_client_prefix_text_ignores_non_strings() -> None:
    assert client_prefix_text(None) == "-"
    assert client_prefix_text(1234) == "-"


def test_installed_filters_truncate_and_redact(caplog: pytest.LogCaptureFixture) -> None:
    install_log_redaction()
    install_log_redaction()  # idempotent
    logger = logging.getLogger(ACCESS_LOGGER)
    assert sum(isinstance(f, TruncateClientAddress) for f in logger.filters) == 1
    token = new_token()
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)
    logger.info(ACCESS_FORMAT, "203.0.113.77:51234", "GET", f"/invite/{token}", "1.1", 200)
    assert "203.0.113.0/24 - " in caplog.text
    assert "203.0.113.77" not in caplog.text and "51234" not in caplog.text
    assert token not in caplog.text
    assert "/invite/[redacted]" in caplog.text


def test_real_uvicorn_server_logs_no_full_ip(caplog: pytest.LogCaptureFixture) -> None:
    settings = load_settings(
        {
            "DATABASE_URL": UNREACHABLE_DB,
            "SECRET_KEY": "s" * 44,
            "FINGERPRINT_KEY": "f" * 44,
            "MFA_KEY": "m" * 44,
        }
    )
    app = create_app(settings, run_retention=False)
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None, access_log=True)
    server = uvicorn.Server(config)
    caplog.set_level(logging.INFO, logger=ACCESS_LOGGER)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "uvicorn did not start"
            assert thread.is_alive(), "uvicorn stopped"
            time.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        token = new_token()
        # /healthz needs no database; /invite/<token>/x is a 404 (no route), also no database.
        for path in ("/healthz", f"/invite/{token}/x"):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5).close()  # noqa: S310
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    lines = [r.getMessage() for r in caplog.records if r.name == ACCESS_LOGGER]
    assert len(lines) == 2, lines
    for line in lines:
        assert line.startswith("127.0.0.0/24 - "), line
        assert "127.0.0.1" not in line
        assert token not in line
    assert "/invite/[redacted]" in lines[1]
