from __future__ import annotations

from pathlib import Path

import yaml
from starlette.testclient import TestClient

from drawbridge.gateway import create_app


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
