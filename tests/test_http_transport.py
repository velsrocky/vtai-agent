"""The HTTP transport is a security-relevant surface: it must refuse to bind
a non-localhost address without a bearer token, and it must reject a
request that does not carry the token. It was documented but untested."""

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest

TOKEN = "test-token-value"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def http_server(sandbox):
    """A live MCP-over-HTTP server on an ephemeral localhost port."""
    import uvicorn

    from vtai_agent.cli import _http_app

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        _http_app("127.0.0.1", TOKEN), host="127.0.0.1", port=port,
        log_level="error",
    ))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.time() + 30
    while time.time() < deadline and not server.started:
        time.sleep(0.1)
    if not server.started:
        pytest.fail("the HTTP server did not start in time")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=15)


def _post(base: str, body: dict, token: str | None):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{base}/mcp", data=json.dumps(body).encode(), method="POST",
        headers=headers,
    )
    return urllib.request.urlopen(req, timeout=20)


def test_refuses_to_bind_a_remote_address_without_a_token(capsys, monkeypatch):
    from vtai_agent import cli

    monkeypatch.delenv("VT_HTTP_TOKEN", raising=False)

    class Args:
        transport = "http"
        host = "0.0.0.0"
        port = 8765
        token = None

    rc = cli.cmd_serve(Args())
    assert rc == 2
    assert "refusing to bind a non-localhost address" in capsys.readouterr().err


def test_http_rejects_a_request_without_the_token(http_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(http_server, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {}}, token=None)
    assert exc.value.code == 401


def test_http_rejects_a_request_with_the_wrong_token(http_server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _post(http_server, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {}}, token="not-the-token")
    assert exc.value.code == 401


def test_http_serves_an_mcp_session_with_the_token(http_server):
    with _post(http_server, {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }, token=TOKEN) as response:
        body = response.read().decode()
    assert '"result"' in body
    assert "vtai-agent" in body
