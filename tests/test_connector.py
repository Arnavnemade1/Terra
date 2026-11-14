"""Security regressions use invented canaries, never real credentials."""
from urllib.parse import urlsplit

import httpx
import pytest

from terra import connector

CANARY = "test-credential-canary-not-a-real-key"


@pytest.mark.parametrize("url", [
    "http://api.openai.com/v1/responses", "https://127.0.0.1/private", "https://169.254.169.254/latest/meta-data/",
    "https://api.openai.com.attacker.example/v1/responses", "https://api.openai.com:8443/v1/responses",
    "https://user:password@api.openai.com/v1/responses", "file:///etc/passwd",
])
def test_untrusted_urls_are_rejected_before_network_access(url):
    with pytest.raises(connector.ConnectorError, match="destination is not permitted"):
        connector.validate_url("openai", url)


async def test_repeated_requests_reuse_one_connection_pool():
    first = connector._async_client()
    assert connector._async_client() is first
    await connector.close()
    assert first.is_closed


async def test_earthdata_credentials_are_stripped_at_cloudfront_redirect(monkeypatch):
    seen = []
    def handler(request):
        seen.append((request.url.host, request.headers.get("authorization")))
        if len(seen) == 1:
            return httpx.Response(303, headers={"location": "https://d1mv8zhcvry6x4.cloudfront.net/product.h5?signature=example"})
        return httpx.Response(206, content=b"\x89HDF\r\n\x1a\n")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(connector, "_async_client", lambda: client)
        response = await connector.request("earthdata", "GET", "https://nisar.asf.earthdatacloud.nasa.gov/product.h5",
                                           headers={"Authorization": f"Bearer {CANARY}"}, follow_redirects=True)
    assert response.status_code == 206
    assert seen[0][1] == f"Bearer {CANARY}"
    assert seen[1][1] is None


async def test_redirect_cannot_escape_provider_allowlist(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1:8000/.env"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(connector, "_async_client", lambda: client)
        with pytest.raises(connector.ConnectorError):
            await connector.request("asf", "GET", "https://api.daac.asf.alaska.edu/services/search/param", follow_redirects=True)
    assert len(calls) == 1


async def test_upstream_error_body_and_signed_url_do_not_escape(monkeypatch):
    def handler(request):
        return httpx.Response(401, json={"error": f"Credential {CANARY} rejected"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(connector, "_async_client", lambda: client)
        with pytest.raises(connector.ConnectorError) as error:
            await connector.request("openai", "POST", f"https://api.openai.com/v1/responses?secret={CANARY}")
    assert CANARY not in str(error.value)
    assert "api.openai.com" not in str(error.value)
    assert "HTTP 401" in str(error.value)


async def test_transport_exception_is_sanitized(monkeypatch):
    def handler(request):
        raise httpx.ConnectError(f"Could not connect to {request.url}")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(connector, "_async_client", lambda: client)
        with pytest.raises(connector.ConnectorError) as error:
            await connector.request("earthdata", "GET", f"https://d1mv8zhcvry6x4.cloudfront.net/a?signature={CANARY}")
    assert CANARY not in str(error.value)
    assert error.value.__suppress_context__


async def test_response_size_is_bounded(monkeypatch):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=b"x" * 100))) as client:
        monkeypatch.setattr(connector, "_async_client", lambda: client)
        with pytest.raises(connector.ConnectorError, match="size limit"):
            await connector.request("usgs", "GET", "https://earthquake.usgs.gov/feed", max_bytes=10)


async def test_cookies_cannot_be_forwarded_by_callers():
    with pytest.raises(connector.ConnectorError, match="headers are not permitted"):
        await connector.request("asf", "GET", "https://api.daac.asf.alaska.edu/services/search/param", headers={"Cookie": CANARY})


def test_diagnostics_contain_only_provider_metrics():
    data = connector.diagnostics()
    assert CANARY not in str(data)
    assert data["credentials"] == "server only"
    for service in data["services"].values():
        assert set(service) == {"label", "requests", "failures", "last_ms", "last_status"}
