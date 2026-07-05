from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.gateway_errors import UpstreamHTTPError
from src.gateway_proxy import NativeProxyClient


def test_curl_upstream_http_error_preserves_status_and_detail(monkeypatch):
    """Regression: curl transport must not double-pass upstream_status."""

    def fake_run(*args, **kwargs):
        return SimpleNamespace(
            stdout=b'{"error":{"message":"not supported"}}\n__HTTP_CODE__404',
            stderr=b'',
            returncode=0,
        )

    monkeypatch.setattr("subprocess.run", fake_run)
    client = NativeProxyClient(base_url="http://upstream.local", api_key="", model="test-model")

    with pytest.raises(UpstreamHTTPError) as exc_info:
        client._do_request_once("POST", "http://upstream.local/v1/assistants", {}, b"{}")

    assert exc_info.value.upstream_status == 404
    assert exc_info.value.detail == {"error": {"message": "not supported"}}
    assert str(exc_info.value) == "upstream HTTP 404"
