from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml
from starlette.testclient import TestClient

from drawbridge.gateway import create_app
from drawbridge.storage import Database


def test_gateway_rejects_anonymous_and_exposes_catalog(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "state_dir": str(tmp_path / "state"),
                "auth": {"mode": "token", "token": "gateway-test-token", "allowed_hosts": ["localhost"]},
                "allowed_client_cidrs": ["127.0.0.0/8"],
                "allow_simulation": True,
            }
        ),
        encoding="utf-8",
    )
    with TestClient(create_app(config), base_url="http://localhost:8787", client=("127.0.0.1", 50000)) as client:
        anonymous = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert anonymous.status_code == 401

        headers = {"Authorization": "Bearer gateway-test-token", "Accept": "application/json, text/event-stream"}
        initialized = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "gateway-test", "version": "1"},
                },
            },
            headers=headers,
        )
        assert initialized.status_code == 200
        assert "ops_release_plan" in initialized.text
        assert "ops_release_status" in initialized.text

        listed = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
            headers=headers,
        )
        assert listed.status_code == 200
        assert "Queue deployment of an existing plan_id" in listed.text
        assert "other methods require idempotency_key" in listed.text

        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "ops_catalog", "arguments": {}},
            },
            headers=headers,
        )
        assert response.status_code == 200
        assert "ops_release_apply" in response.text
        assert "ops_http_request" in response.text
        assert "getting_started" in response.text
        assert "ops_workflow_run" in response.text


def test_gateway_allows_a_configured_direct_host(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "state_dir": str(tmp_path / "state"),
                "auth": {
                    "mode": "token",
                    "token": "gateway-test-token",
                    "allowed_hosts": ["192.168.0.67"],
                },
                "allowed_client_cidrs": ["127.0.0.0/8"],
                "allow_simulation": True,
            }
        ),
        encoding="utf-8",
    )
    with TestClient(
        create_app(config),
        base_url="http://192.168.0.67:8787",
        client=("127.0.0.1", 50000),
    ) as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            headers={
                "Authorization": "Bearer gateway-test-token",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert response.status_code == 200


def test_gateway_initializes_database_once_for_concurrent_tool_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "state_dir": str(tmp_path / "state"),
                "auth": {"mode": "token", "token": "gateway-test-token", "allowed_hosts": ["localhost"]},
                "allowed_client_cidrs": ["127.0.0.0/8"],
                "allow_simulation": True,
            }
        ),
        encoding="utf-8",
    )
    initialize_calls = 0
    close_calls = 0
    original_initialize = Database.initialize
    original_close = Database.close

    async def counted_initialize(database: Database) -> None:
        nonlocal initialize_calls
        initialize_calls += 1
        await original_initialize(database)

    async def counted_close(database: Database) -> None:
        nonlocal close_calls
        close_calls += 1
        await original_close(database)

    monkeypatch.setattr(Database, "initialize", counted_initialize)
    monkeypatch.setattr(Database, "close", counted_close)
    headers = {"Authorization": "Bearer gateway-test-token", "Accept": "application/json, text/event-stream"}
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "ops_catalog", "arguments": {}},
    }

    with TestClient(create_app(config), base_url="http://localhost:8787", client=("127.0.0.1", 50000)) as client:
        with ThreadPoolExecutor(max_workers=8) as executor:
            responses = list(executor.map(lambda _: client.post("/mcp", json=request, headers=headers), range(8)))

        assert all(response.status_code == 200 for response in responses)
        assert initialize_calls == 1

    assert close_calls == 1
