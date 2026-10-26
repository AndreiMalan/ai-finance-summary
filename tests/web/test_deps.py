"""Unit tests for request helpers in statbrief.web.deps."""
from types import SimpleNamespace

from starlette.requests import Request

from statbrief.web.deps import client_ip


def _request(headers: dict[str, str] | None = None, client: tuple[str, int] | None = ("9.9.9.9", 1234)) -> Request:
    """Build a bare ASGI request with the given headers and peer address."""
    scope = {
        "type": "http", "method": "GET", "path": "/", "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": client,
    }
    return Request(scope)


def test_client_ip_takes_first_forwarded_hop():
    """With several X-Forwarded-For hops the first (the client, as set by Vercel) wins, trimmed."""
    req = _request({"X-Forwarded-For": " 1.2.3.4 , 10.0.0.1, 10.0.0.2"})
    assert client_ip(req) == "1.2.3.4"


def test_client_ip_falls_back_to_peer_without_header():
    """Without X-Forwarded-For the socket peer address is used."""
    assert client_ip(_request()) == "9.9.9.9"


def test_client_ip_unknown_without_peer():
    """No header and no peer information yields the "unknown" placeholder."""
    req = _request(client=None)
    assert req.client is None and client_ip(req) == "unknown"
    assert client_ip(SimpleNamespace(headers={}, client=None)) == "unknown"
