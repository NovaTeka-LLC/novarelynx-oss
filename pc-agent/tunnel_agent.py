"""Minimal PC-agent tunnel client: connects to the relay server's tunnel
WebSocket, and for every http.request it receives, actually makes that
request against a local port and sends back the real response. This is the
proof-of-concept for M2 -- the real PC agent (Wails tray app etc.) will wrap
this same core loop with a UI, but the loop itself is the whole mechanism.

Also relays real bidirectional WebSocket connections (ws.open / ws.message /
ws.close) alongside the http.request/http.response traffic, over this SAME
control connection -- not a second socket. This is what lets a browser's own
WS connection to a tunneled app (e.g. AionUi's live-chat socket, which always
opens wss://<host>/ws relative to whatever domain it's loaded from) actually
work through the tunnel: on ws.open, this opens a REAL local WebSocket
connection to the app (same local_base_url, ws(s):// instead of http(s)://)
and pumps frames both directions until either side closes.

Usage:
    python tunnel_agent.py <relay_ws_url> <tunnel_token> <local_base_url>

Example (tunnel a locally running dashboard on port 8005):
    python tunnel_agent.py ws://127.0.0.1:8080/api/v1/tunnel/ws <token> http://127.0.0.1:8005
"""
import asyncio
import base64
import json
import sys

import httpx
import websockets

RECONNECT_MIN_SECONDS = 2
RECONNECT_MAX_SECONDS = 30
# http.response bodies are sent as one base64 JSON frame (see
# _handle_http_request) with no chunking, so this has to cover the largest
# file a tunneled app might serve whole -- e.g. the storefront's ~66MB
# installer, which base64-inflates to ~88MB. Matches --ws-max-size on the
# relay server (server/Dockerfile).
WS_MAX_SIZE = 150 * 1024 * 1024
# The relay already strips these before sending ws.open (they belong to the
# BROWSER's handshake with the relay, not this agent's own separate handshake
# with the local app), but filtered again here too -- defense in depth, this
# script isn't the only thing that could ever send a ws.open message.
_WS_HANDSHAKE_ONLY_HEADERS = {
    "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
    "sec-websocket-protocol", "sec-websocket-accept", "origin", "host", "connection", "upgrade",
}


def _to_ws_base_url(http_base_url: str) -> str:
    """"http://127.0.0.1:25808" -> "ws://127.0.0.1:25808", https:// -> wss://."""
    if http_base_url.startswith("https://"):
        return "wss://" + http_base_url[len("https://"):]
    if http_base_url.startswith("http://"):
        return "ws://" + http_base_url[len("http://"):]
    return http_base_url


def _normalize_local_url(raw: str) -> str:
    """A local target with no scheme (e.g. "localhost:3000" typed instead of
    "http://localhost:3000") makes httpx.AsyncClient's base_url raise
    immediately with a hard-to-diagnose "missing http:// protocol" error --
    confirmed for real 2026-07-21, a beta tester's app was broken exactly
    this way with no way for anyone but them to fix it after the fact. Fixed
    at the one place this value enters the program instead of trusting every
    caller (the dashboard's own add-app form already normalizes bare ports,
    but this script is also runnable directly / from admin_cli-registered
    apps, so the same guard belongs here too)."""
    raw = raw.strip()
    if "://" not in raw:
        return f"http://{raw}"
    return raw


async def run_agent(relay_ws_url: str, tunnel_token: str, local_base_url: str) -> None:
    local_base_url = _normalize_local_url(local_base_url)
    local_ws_base_url = _to_ws_base_url(local_base_url)
    """Reconnects forever with capped exponential backoff. Confirmed real
    2026-07-17: a server-side restart (redeploy) drops this connection and,
    without this loop, the whole process just exited -- meaning every relay
    redeploy silently took every tunnel offline until someone noticed and
    relaunched it by hand. This is the actual production requirement for a
    client meant to run unattended."""
    url = f"{relay_ws_url}?token={tunnel_token}"
    delay = RECONNECT_MIN_SECONDS
    while True:
        # ws_conns: conn_id -> the real local WebSocket connection this agent
        # opened for a browser's relayed WS. ws_tasks: conn_id -> the task
        # pumping frames FROM that local connection back upstream (see
        # _handle_ws_open) -- cancelled on disconnect so a dropped control
        # connection doesn't leave orphaned local WS connections/tasks running.
        ws_conns: dict[str, "websockets.ClientConnection"] = {}
        ws_tasks: dict[str, asyncio.Task] = {}
        try:
            async with websockets.connect(url, max_size=WS_MAX_SIZE) as ws:
                print(f"[agent] connected, proxying to {local_base_url}")
                delay = RECONNECT_MIN_SECONDS  # reset backoff once a connection actually succeeds
                # Multiple coroutines (the main loop below, and one per relayed WS
                # connection in _handle_ws_open) all send on this SAME control
                # socket now -- a lock keeps concurrent sends from interleaving
                # and corrupting frames, which was never a risk before when
                # everything was strictly sequential (one request in, one
                # response out, nothing else ever touched `ws.send`).
                send_lock = asyncio.Lock()
                async with httpx.AsyncClient(base_url=local_base_url, follow_redirects=False) as client:
                    async for raw in ws:
                        await _dispatch(ws, send_lock, client, local_ws_base_url, ws_conns, ws_tasks, raw)
        except (websockets.exceptions.ConnectionClosed, OSError) as e:
            print(f"[agent] disconnected ({e}), retrying in {delay}s...")
        except Exception as e:
            print(f"[agent] unexpected error ({e}), retrying in {delay}s...")
        finally:
            for task in ws_tasks.values():
                task.cancel()
        await asyncio.sleep(delay)
        delay = min(delay * 2, RECONNECT_MAX_SECONDS)


async def _dispatch(ws, send_lock: asyncio.Lock, client: httpx.AsyncClient, local_ws_base_url: str,
                    ws_conns: dict, ws_tasks: dict, raw: str) -> None:
    msg = json.loads(raw)
    msg_type = msg.get("type")
    if msg_type == "http.request":
        await _handle_http_request(ws, send_lock, client, msg)
    elif msg_type == "ws.open":
        conn_id = msg["conn_id"]
        ws_tasks[conn_id] = asyncio.create_task(
            _handle_ws_open(ws, send_lock, local_ws_base_url, ws_conns, ws_tasks, msg))
    elif msg_type == "ws.message":
        await _handle_ws_message_from_relay(ws_conns, msg)
    elif msg_type == "ws.close":
        await _handle_ws_close_from_relay(ws_conns, msg)


async def _handle_http_request(ws, send_lock: asyncio.Lock, client: httpx.AsyncClient, req: dict) -> None:
    request_id = req["request_id"]
    try:
        resp = await client.request(
            req["method"], req["path"],
            headers={k: v for k, v in req.get("headers", {}).items()},
            content=base64.b64decode(req.get("body_b64", "")),
        )
        payload = {
            "type": "http.response", "request_id": request_id,
            "status": resp.status_code,
            "headers": {k: v for k, v in resp.headers.items()
                       if k.lower() not in ("content-length", "content-encoding", "transfer-encoding")},
            "body_b64": base64.b64encode(resp.content).decode("ascii"),
        }
    except Exception as e:
        payload = {
            "type": "http.response", "request_id": request_id,
            "status": 502, "headers": {"content-type": "text/plain"},
            "body_b64": base64.b64encode(f"tunnel agent: local request failed: {e}".encode()).decode("ascii"),
        }
    async with send_lock:
        await ws.send(json.dumps(payload))


async def _handle_ws_open(ws, send_lock: asyncio.Lock, local_ws_base_url: str, ws_conns: dict,
                          ws_tasks: dict, msg: dict) -> None:
    """Opens a REAL local WebSocket connection to the tunneled app for one
    browser-side connection, confirms it (or reports failure) back upstream,
    then pumps every frame the local app sends back up as ws.message for the
    rest of this connection's life. The reverse direction (browser -> local
    app) is handled by _handle_ws_message_from_relay writing to ws_conns."""
    conn_id = msg["conn_id"]
    target = local_ws_base_url.rstrip("/") + msg.get("path", "/")
    headers = [(k, v) for k, v in msg.get("headers", {}).items() if k.lower() not in _WS_HANDSHAKE_ONLY_HEADERS]
    try:
        local_ws = await websockets.connect(target, additional_headers=headers, open_timeout=10)
    except Exception as e:
        async with send_lock:
            await ws.send(json.dumps({"type": "ws.open_failed", "conn_id": conn_id, "reason": str(e)}))
        return
    ws_conns[conn_id] = local_ws
    async with send_lock:
        await ws.send(json.dumps({"type": "ws.opened", "conn_id": conn_id}))
    try:
        async for frame in local_ws:
            binary = isinstance(frame, (bytes, bytearray))
            data_b64 = base64.b64encode(frame if binary else frame.encode()).decode("ascii")
            async with send_lock:
                await ws.send(json.dumps({"type": "ws.message", "conn_id": conn_id,
                                          "data_b64": data_b64, "binary": binary}))
    except Exception:
        pass  # local app closed/dropped the connection -- fall through to the close notice below
    finally:
        ws_conns.pop(conn_id, None)
        ws_tasks.pop(conn_id, None)
        try:
            await local_ws.close()
        except Exception:
            pass
        try:
            async with send_lock:
                await ws.send(json.dumps({"type": "ws.close", "conn_id": conn_id, "code": 1000}))
        except Exception:
            pass  # control connection itself is already gone -- nothing to tell


async def _handle_ws_message_from_relay(ws_conns: dict, msg: dict) -> None:
    """Browser -> relay -> here: write the frame to the matching local WS."""
    local_ws = ws_conns.get(msg["conn_id"])
    if local_ws is None:
        return
    data = base64.b64decode(msg.get("data_b64", ""))
    try:
        await local_ws.send(data if msg.get("binary") else data.decode("utf-8", errors="replace"))
    except Exception:
        pass


async def _handle_ws_close_from_relay(ws_conns: dict, msg: dict) -> None:
    """The browser (or the relay, on its behalf) closed -- close our matching
    real local WS connection too. _handle_ws_open's own finally block is what
    reports the close back upstream if the LOCAL side closes first instead."""
    local_ws = ws_conns.pop(msg["conn_id"], None)
    if local_ws is not None:
        try:
            await local_ws.close(code=msg.get("code") or 1000)
        except Exception:
            pass


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    asyncio.run(run_agent(sys.argv[1], sys.argv[2], sys.argv[3]))
