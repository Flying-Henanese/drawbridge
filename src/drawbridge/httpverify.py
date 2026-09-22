from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from urllib.parse import urlsplit

import httpx

from .config import HttpVerifyConfig
from .errors import DrawbridgeError
from .models import HttpRequestInput


class HttpVerifier:
    def __init__(self, config: HttpVerifyConfig, *, trusted_health_check: bool = False) -> None:
        self.config = config
        self.trusted_health_check = trusted_health_check

    async def request(self, request: HttpRequestInput) -> dict[str, object]:
        parsed, addresses = await self._validate_target(request.url)
        timeout = min(request.timeout_seconds, self.config.timeout_seconds)
        if request.method in {"GET", "HEAD"} and request.body is not None:
            raise DrawbridgeError("INVALID_PARAMETER", "GET/HEAD requests cannot contain a body")
        headers = dict(request.headers)
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout),
                follow_redirects=False,
                trust_env=False,
                verify=True,
            ) as client:
                response = await client.request(
                    request.method,
                    request.url,
                    headers=headers,
                    content=request.body.encode("utf-8") if request.body is not None else None,
                )
                chunks: list[bytes] = []
                total = 0
                truncated = False
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    remaining = self.config.max_response_bytes - sum(len(item) for item in chunks)
                    if remaining > 0:
                        chunks.append(chunk[:remaining])
                    if total > self.config.max_response_bytes:
                        truncated = True
                        break
                content = b"".join(chunks)
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise DrawbridgeError("TIMEOUT", "HTTP verification timed out", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise DrawbridgeError("VERIFY_FAILED", f"HTTP verification failed: {exc}", retryable=True) from exc
        elapsed_ms = int((time.monotonic() - started) * 1000)
        try:
            text = content.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        lines = text.splitlines()[:200]
        return {
            "url": request.url,
            "method": request.method,
            "resolved_addresses": [str(address) for address in addresses],
            "status_code": response.status_code,
            "headers": {
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower() in {"content-type", "content-length", "etag", "last-modified", "cache-control"}
            },
            "elapsed_ms": elapsed_ms,
            "content": text,
            "lines": lines,
            "truncated": truncated,
            "untrusted": True,
        }

    async def _validate_target(self, url: str) -> tuple[object, list[ipaddress._BaseAddress]]:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise DrawbridgeError("TARGET_NOT_ALLOWED", "only http and https URLs are supported")
        if parsed.username or parsed.password or parsed.fragment:
            raise DrawbridgeError("TARGET_NOT_ALLOWED", "userinfo and fragments are not allowed")
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise DrawbridgeError("TARGET_NOT_ALLOWED", "invalid URL port") from exc
        if port not in self.config.allowed_ports and not self.trusted_health_check:
            raise DrawbridgeError("TARGET_NOT_ALLOWED", "target port is not allowed")
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, parsed.hostname, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise DrawbridgeError("TARGET_NOT_ALLOWED", "target DNS resolution failed") from exc
        addresses: list[ipaddress._BaseAddress] = []
        for info in infos:
            try:
                address = ipaddress.ip_address(info[4][0])
            except ValueError as exc:
                raise DrawbridgeError("TARGET_NOT_ALLOWED", "target resolved to an invalid address") from exc
            addresses.append(address)
        if not addresses:
            raise DrawbridgeError("TARGET_NOT_ALLOWED", "target has no addresses")
        if not self.trusted_health_check:
            networks = [ipaddress.ip_network(value, strict=False) for value in self.config.allowed_cidrs]
            if not networks or any(not any(address in network for network in networks) for address in addresses):
                raise DrawbridgeError("TARGET_NOT_ALLOWED", "all resolved target addresses must be allowed")
        return parsed, list(dict.fromkeys(addresses))
