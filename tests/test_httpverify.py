from __future__ import annotations

import pytest

from drawbridge.config import HttpVerifyConfig
from drawbridge.errors import DrawbridgeError
from drawbridge.httpverify import HttpVerifier
from drawbridge.models import HttpRequestInput


@pytest.mark.asyncio
async def test_http_verifier_rejects_disallowed_port_before_connecting() -> None:
    verifier = HttpVerifier(HttpVerifyConfig(allowed_cidrs=["127.0.0.1/32"], allowed_ports=[8080]))
    request = HttpRequestInput(url="http://127.0.0.1:9999/health")

    with pytest.raises(DrawbridgeError) as caught:
        await verifier.request(request)

    assert caught.value.code == "TARGET_NOT_ALLOWED"


@pytest.mark.asyncio
async def test_http_verifier_rejects_userinfo_and_fragment() -> None:
    verifier = HttpVerifier(HttpVerifyConfig(allowed_cidrs=["127.0.0.1/32"], allowed_ports=[80]))

    for url in ("http://user:pass@127.0.0.1/health", "http://127.0.0.1/health#secret"):
        with pytest.raises(DrawbridgeError) as caught:
            await verifier.request(HttpRequestInput(url=url))
        assert caught.value.code == "TARGET_NOT_ALLOWED"
