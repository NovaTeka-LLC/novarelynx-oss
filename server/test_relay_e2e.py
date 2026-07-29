"""Real end-to-end test for the self-host relay: starts the actual server,
registers an app self-serve, connects a fake tunnel_agent over the real
tunnel WebSocket, sends a real HTTP request through the gateway (login form
+ session cookie, exactly as a browser would), and confirms the response
came back through the whole relay path -- not mocked at any layer.
stdlib-only (urllib + http.cookiejar) plus websockets, which relay.py already
requires -- no extra test-only dependency for a repo that otherwise has none.

Run directly:
    python test_relay_e2e.py
"""
import base64
import http.cookiejar
import json
import os
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import uvicorn
from websockets.sync.client import connect as ws_connect

_DB = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["RELAY_DB_FILE"] = _DB
os.environ["RELAY_ADMIN_SECRET"] = "test-admin-secret"
os.environ["RELAY_BASE_DOMAIN"] = "example.com"
os.environ["RELAY_COOKIE_SECURE"] = "0"  # this test runs plain http://

import relay  # noqa: E402  (env vars above must be set before import -- relay.py bootstraps at import time)

BASE = "http://127.0.0.1:8099"
WS_BASE = "ws://127.0.0.1:8099"

# A gateway subdomain (e.g. "swkw-....example.com") doesn't really resolve
# anywhere -- fake DNS for it so the request actually connects to this test
# server, exactly like curl's --resolve. This matters because the session
# cookie's Domain=.example.com only gets accepted/resent by a proper
# domain-matching cookie jar if the request's real host is that same domain,
# not a spoofed Host header on a request that actually went to 127.0.0.1.
_real_getaddrinfo = socket.getaddrinfo


def _fake_getaddrinfo(host, *args, **kwargs):
    if host.endswith(".example.com"):
        host = "127.0.0.1"
    return _real_getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _fake_getaddrinfo

_cookie_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_cookie_jar))


def _request(method: str, path: str, host: str | None = None, api_key: str | None = None,
            json_body: dict | None = None, form_body: dict | None = None) -> tuple[int, str]:
    base = f"http://{host}:8099" if host else BASE
    headers = {}
    if api_key:
        headers["X-Api-Key"] = api_key
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    elif form_body is not None:
        data = urllib.parse.urlencode(form_body).encode()
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with _opener.open(req, timeout=5) as resp:
            return resp.status, resp.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")


def _run_server():
    uvicorn.run(relay.app, host="127.0.0.1", port=8099, log_level="warning")


def main() -> None:
    thread = threading.Thread(target=_run_server, daemon=True)
    thread.start()
    for _ in range(50):
        try:
            status, _ = _request("GET", "/api/v1/health")
            if status == 200:
                break
        except Exception:
            pass
        time.sleep(0.1)
    else:
        raise SystemExit("server never came up")
    print("[OK] server up, health check passed")

    # First-run bootstrap created exactly one account -- grab its real credentials
    # straight from server state, same as a fresh `docker compose up` would print.
    assert len(relay.users_by_subdomain) == 1, "expected exactly one bootstrapped account"
    user = next(iter(relay.users_by_subdomain.values()))
    subdomain, api_key = user.subdomain, user.api_key
    print(f"[OK] first-run bootstrap created one account: {subdomain}")

    status, _ = _request("POST", "/api/v1/users/create", json_body={"admin_secret": "wrong"})
    assert status == 403, f"expected 403, got {status}"
    print("[OK] wrong admin secret rejected")

    status, body = _request("POST", "/api/v1/my/apps", api_key=api_key,
                            json_body={"name": "Test App", "entry_path": "/"})
    assert status == 200, body
    data = json.loads(body)
    assert data["app_name"] == "test-app", f"expected slugified name, got {data['app_name']!r}"
    tunnel_token = data["tunnel_token"]
    print(f"[OK] self-serve app registration -> app_name={data['app_name']!r}")

    status, body = _request("GET", "/api/v1/my/apps", api_key=api_key)
    assert status == 200 and len(json.loads(body)) == 1
    print("[OK] list_my_apps shows the new app")

    status, _ = _request("GET", "/api/v1/my/account", api_key="not-a-real-key")
    assert status == 401, f"expected 401, got {status}"
    print("[OK] invalid api key rejected")

    # Connect a fake tunnel_agent -- the real /api/v1/tunnel/ws control socket,
    # answering http.request with a canned http.response, exactly like a real
    # tunnel_agent.py proxying to a real local app would.
    ws = ws_connect(f"{WS_BASE}/api/v1/tunnel/ws?token={tunnel_token}")
    print("[OK] fake tunnel_agent connected over the real control socket")

    def fake_agent_loop():
        while True:
            try:
                raw = ws.recv(timeout=5)
            except Exception:
                return
            msg = json.loads(raw)
            if msg.get("type") == "http.request":
                ws.send(json.dumps({
                    "type": "http.response", "request_id": msg["request_id"],
                    "status": 200, "headers": {"content-type": "text/plain"},
                    "body_b64": base64.b64encode(b"hello from fake local app").decode(),
                }))

    agent_thread = threading.Thread(target=fake_agent_loop, daemon=True)
    agent_thread.start()
    time.sleep(0.3)  # let the server's tunnel_ws handler register tunnel.ws before we route a request at it

    host = f"{subdomain}.example.com"
    status, body = _request("GET", "/", host=host)
    assert status == 401 and "Sign in" in body, "expected login form for an unauthenticated visit"
    print("[OK] unauthenticated gateway visit shows the login form")

    status, _ = _request("POST", "/_gateway_login", host=host, form_body={"api_key": api_key})
    assert status == 200
    print("[OK] gateway login succeeded, session cookie set")

    status, body = _request("GET", "/test-app/", host=host)
    assert status == 200, body
    assert body == "hello from fake local app", f"unexpected body: {body!r}"
    print("[OK] full tunnel round trip: gateway -> WS control socket -> fake local app -> back")

    ws.close()
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
