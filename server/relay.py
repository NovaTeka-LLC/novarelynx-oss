"""NovaRelynx relay server -- self-host edition.

Exposes local ports on your own machine as public HTTPS URLs. A PC agent
(see ../pc-agent/) keeps one outbound WebSocket connection open to this
server; this server forwards HTTP requests and relayed WebSocket frames
over it in both directions. No inbound connection to your machine is ever
needed.

This is the single-owner/self-host edition: one relay server you run
yourself, with no billing, plans, or usage caps -- those only make sense
for a multi-tenant hosted service (see https://novarelynx.com if you'd
rather not run this yourself). Everyone who has an account on your own
relay server is assumed trusted, since you're the one who created it.

Run directly:
    pip install -r requirements.txt
    python relay.py

Or via Docker:
    docker compose up

On first run, if there are no accounts yet, one is created automatically
and its subdomain + API key are printed to stdout -- paste the API key
into the PC agent's dashboard (http://127.0.0.1:8787 after you run it) to
get started. No sign-up flow, no external accounts needed.
"""
import asyncio
import base64
import os
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass, field

from fastapi import Cookie, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

REQUEST_TIMEOUT_SECONDS = 90
SESSION_TTL_SECONDS = 24 * 3600
SESSION_COOKIE = "novarelynx_session"
# Only turn this off for local http:// testing -- browsers refuse to store/send
# a Secure cookie over plain http, so it has to be togglable for that case, but
# a real deployment behind Caddy/nginx TLS should always leave this on.
SESSION_COOKIE_SECURE = os.environ.get("RELAY_COOKIE_SECURE", "1") != "0"
# Gates the admin-only account/app-management routes (create account, register
# app, list/delete/rotate-key) -- generate a real random value for any
# deployment reachable from the internet; the default only exists so a fresh
# clone doesn't immediately 500 on missing config.
ADMIN_SECRET = os.environ.get("RELAY_ADMIN_SECRET", "change-me-before-deploying")
# Configurable so local testing can use "xxxx.localhost" instead of a real domain.
BASE_DOMAIN = os.environ.get("RELAY_BASE_DOMAIN", "localhost")
# The PC agent's control WebSocket needs its own stable hostname, separate
# from any user's gateway subdomain -- permanently allowed independent of
# who's registered.
CONTROL_SUBDOMAIN = os.environ.get("RELAY_CONTROL_SUBDOMAIN", "relay")

# No 0/O/1/I/l -- gateway subdomains get typed by hand often enough that visual
# ambiguity is worth avoiding.
_SUBDOMAIN_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


def _gen_subdomain() -> str:
    return "-".join("".join(secrets.choice(_SUBDOMAIN_ALPHABET) for _ in range(4)) for _ in range(4))


def _gen_unique_subdomain() -> str:
    sub = _gen_subdomain()
    while sub in users_by_subdomain or sub in apps_by_dedicated_subdomain:
        sub = _gen_subdomain()
    return sub


def subdomain_from_host(host: str) -> str | None:
    host = host.split(":")[0].lower()
    suffix = "." + BASE_DOMAIN
    if not host.endswith(suffix):
        return None
    subdomain = host[: -len(suffix)]
    return subdomain or None


# App names double as URL path segments (https://<user>.<domain>/<name>/...),
# so they're slugified to a safe alphabet rather than rejecting anything not
# already URL-safe.
_SLUG_INVALID_RE = re.compile(r"[^a-z0-9]+")
RESERVED_APP_NAMES = {"_gateway_login", "api"}


def _slugify_app_name(raw: str) -> str:
    return _SLUG_INVALID_RE.sub("-", raw.strip().lower()).strip("-")


def _validate_app_name(raw: str) -> str:
    slug = _slugify_app_name(raw)
    if not slug:
        raise HTTPException(400, "app name must contain at least one letter or digit")
    if slug in RESERVED_APP_NAMES:
        raise HTTPException(400, f"'{slug}' is a reserved name -- choose another")
    return slug


@dataclass
class AppTunnel:
    name: str  # slugified, unique per owner -- also the routing key: /<name>/... under the owner's subdomain
    owner_subdomain: str
    entry_path: str = "/"  # where this app's real UI lives (e.g. "/ui") -- most apps don't serve it at bare "/"
    # Opt-in exception to path-based routing: set for an app whose own frontend hardcodes a domain-root
    # assumption (e.g. builds its WebSocket URL as wss://window.location.host/ws with no base-path config).
    # When set, the app is ALSO reachable at https://<dedicated_subdomain>.<domain>/... directly, in
    # addition to the normal path-based route, not instead of it.
    dedicated_subdomain: str | None = None
    # Only meaningful with dedicated_subdomain: skips the login gate entirely for this one app, for
    # something meant to be publicly reachable (e.g. your own landing page) rather than private tooling.
    public: bool = False
    ws: WebSocket | None = None
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    # A browser's own WebSocket connection to a tunneled app (not just buffered HTTP request/response) is
    # relayed through this SAME control connection via ws.* message types. conn_id -> the browser-side
    # WebSocket currently being relayed, and conn_id -> the Future ws.opened/ws.open_failed resolves.
    browser_ws: dict[str, WebSocket] = field(default_factory=dict)
    ws_open_pending: dict[str, asyncio.Future] = field(default_factory=dict)


@dataclass
class User:
    subdomain: str  # gateway subdomain
    api_key: str
    apps: list[AppTunnel] = field(default_factory=list)


users_by_subdomain: dict[str, User] = {}
users_by_api_key: dict[str, User] = {}
apps_by_owner_and_name: dict[tuple[str, str], AppTunnel] = {}
apps_by_dedicated_subdomain: dict[str, AppTunnel] = {}
app_tokens: dict[str, tuple[str, str]] = {}  # tunnel_token -> (owner_subdomain, app_name)
sessions: dict[str, tuple[str, float]] = {}  # session_id -> (owner subdomain, expires_at)

# Accounts/apps persist across restarts so the gateway address and API key
# don't churn on every redeploy. Live WebSocket connections obviously can't
# survive a restart -- tunnel_agent.py just reconnects with its already-known
# token once it notices and retries.
_DB_FILE = os.environ.get("RELAY_DB_FILE", "./data/relay.db")


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(_DB_FILE)
    conn.execute("CREATE TABLE IF NOT EXISTS users (subdomain TEXT PRIMARY KEY, api_key TEXT NOT NULL)")
    conn.execute("CREATE TABLE IF NOT EXISTS apps ("
                 "owner_subdomain TEXT NOT NULL, name TEXT NOT NULL, entry_path TEXT NOT NULL, "
                 "dedicated_subdomain TEXT, public INTEGER NOT NULL DEFAULT 0, "
                 "PRIMARY KEY (owner_subdomain, name))")
    conn.execute("CREATE TABLE IF NOT EXISTS app_tokens (token TEXT PRIMARY KEY, "
                 "owner_subdomain TEXT NOT NULL, app_name TEXT NOT NULL)")
    return conn


def _save_user(user: "User") -> None:
    conn = _db()
    try:
        conn.execute(
            "INSERT INTO users (subdomain, api_key) VALUES (?, ?) "
            "ON CONFLICT(subdomain) DO UPDATE SET api_key=excluded.api_key",
            (user.subdomain, user.api_key),
        )
        conn.commit()
    finally:
        conn.close()


def _delete_app_db(owner_subdomain: str, name: str) -> None:
    conn = _db()
    try:
        conn.execute("DELETE FROM apps WHERE owner_subdomain=? AND name=?", (owner_subdomain, name))
        conn.execute("DELETE FROM app_tokens WHERE owner_subdomain=? AND app_name=?", (owner_subdomain, name))
        conn.commit()
    finally:
        conn.close()


def _delete_user_db(subdomain: str) -> None:
    conn = _db()
    try:
        conn.execute("DELETE FROM users WHERE subdomain=?", (subdomain,))
        conn.commit()
    finally:
        conn.close()


def _save_app_row(tunnel: "AppTunnel") -> None:
    conn = _db()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO apps (owner_subdomain, name, entry_path, dedicated_subdomain, public) "
            "VALUES (?, ?, ?, ?, ?)",
            (tunnel.owner_subdomain, tunnel.name, tunnel.entry_path, tunnel.dedicated_subdomain,
             int(tunnel.public)),
        )
        conn.commit()
    finally:
        conn.close()


def _save_app(tunnel: "AppTunnel", token: str) -> None:
    _save_app_row(tunnel)
    conn = _db()
    try:
        conn.execute("INSERT OR REPLACE INTO app_tokens (token, owner_subdomain, app_name) VALUES (?, ?, ?)",
                     (token, tunnel.owner_subdomain, tunnel.name))
        conn.commit()
    finally:
        conn.close()


def _load_state() -> None:
    os.makedirs(os.path.dirname(_DB_FILE) or ".", exist_ok=True)
    conn = _db()
    try:
        for subdomain, api_key in conn.execute("SELECT subdomain, api_key FROM users"):
            user = User(subdomain=subdomain, api_key=api_key)
            users_by_subdomain[subdomain] = user
            users_by_api_key[api_key] = user
        for owner, name, entry_path, dedicated_subdomain, public in conn.execute(
            "SELECT owner_subdomain, name, entry_path, dedicated_subdomain, public FROM apps"
        ):
            tunnel = AppTunnel(name=name, owner_subdomain=owner, entry_path=entry_path,
                               dedicated_subdomain=dedicated_subdomain, public=bool(public))
            apps_by_owner_and_name[(owner, name)] = tunnel
            if dedicated_subdomain:
                apps_by_dedicated_subdomain[dedicated_subdomain] = tunnel
            if owner in users_by_subdomain:
                users_by_subdomain[owner].apps.append(tunnel)
        for token, owner, name in conn.execute("SELECT token, owner_subdomain, app_name FROM app_tokens"):
            app_tokens[token] = (owner, name)
    finally:
        conn.close()


def _bootstrap_first_account() -> None:
    """No Google sign-in, no Stripe, no separate storefront in this edition --
    the entire "getting started" flow is: run this, get an account for free.
    Only runs once, the very first time (an empty users table); afterward use
    the admin routes below (or just log into the gateway page and click
    around) to add more accounts if you want to share this relay."""
    if users_by_subdomain:
        return
    subdomain = _gen_unique_subdomain()
    api_key = secrets.token_urlsafe(24)
    user = User(subdomain=subdomain, api_key=api_key)
    users_by_subdomain[subdomain] = user
    users_by_api_key[api_key] = user
    _save_user(user)
    print("=" * 72)
    print("First run -- created your account:")
    print(f"  subdomain: {subdomain}")
    print(f"  api_key:   {api_key}")
    print("Paste the API key into the PC agent's dashboard (pc-agent/dashboard.py,")
    print("http://127.0.0.1:8787) to start tunneling.")
    print("=" * 72)


_load_state()
_bootstrap_first_account()


class CreateUserRequest(BaseModel):
    admin_secret: str


class CreateUserResponse(BaseModel):
    subdomain: str
    api_key: str


class AppRegisterRequest(BaseModel):
    admin_secret: str
    user_subdomain: str
    app_name: str
    entry_path: str = "/"
    dedicated_subdomain: bool = False
    public: bool = False


class AppRegisterResponse(BaseModel):
    tunnel_token: str
    app_name: str  # slugified, canonical -- may differ from the raw name submitted
    dedicated_subdomain: str | None = None


class SelfAppRegisterRequest(BaseModel):
    name: str
    entry_path: str = "/"
    dedicated_subdomain: bool = False
    public: bool = False


class MyAppInfo(BaseModel):
    name: str
    entry_path: str
    connected: bool  # tunnel agent currently has a live WS open for this app
    dedicated_subdomain: str | None = None


class MyAccountInfo(BaseModel):
    subdomain: str
    api_key: str


class AdminActionRequest(BaseModel):
    admin_secret: str


class AdminUserInfo(BaseModel):
    subdomain: str
    apps: list[MyAppInfo]


class RotateKeyResponse(BaseModel):
    subdomain: str
    api_key: str


def _require_admin(secret: str) -> None:
    if not secrets.compare_digest(secret, ADMIN_SECRET):
        raise HTTPException(403, "invalid admin secret")


def _user_from_api_key(x_api_key: str | None) -> User:
    user = users_by_api_key.get(x_api_key) if x_api_key else None
    if user is None:
        raise HTTPException(401, "invalid api key")
    return user


def _user_from_request(x_api_key: str | None, session_id: str | None) -> User:
    if x_api_key:
        return _user_from_api_key(x_api_key)
    owner = _authed_owner_from_session_cookie(session_id)
    user = users_by_subdomain.get(owner) if owner else None
    if user is None:
        raise HTTPException(401, "not signed in")
    return user


def _create_app_for_user(user: User, name: str, entry_path: str, want_dedicated_subdomain: bool = False,
                         public: bool = False) -> AppRegisterResponse:
    slug = _validate_app_name(name)
    if (user.subdomain, slug) in apps_by_owner_and_name:
        raise HTTPException(409, f"an app named '{slug}' already exists on this account")
    dedicated = _gen_unique_subdomain() if want_dedicated_subdomain else None
    tunnel = AppTunnel(name=slug, owner_subdomain=user.subdomain, entry_path=entry_path or "/",
                       dedicated_subdomain=dedicated, public=public and dedicated is not None)
    apps_by_owner_and_name[(user.subdomain, slug)] = tunnel
    if dedicated:
        apps_by_dedicated_subdomain[dedicated] = tunnel
    user.apps.append(tunnel)
    token = secrets.token_urlsafe(32)
    app_tokens[token] = (user.subdomain, slug)
    _save_app(tunnel, token)
    return AppRegisterResponse(tunnel_token=token, app_name=slug, dedicated_subdomain=dedicated)


async def _delete_app(tunnel: AppTunnel) -> None:
    key = (tunnel.owner_subdomain, tunnel.name)
    apps_by_owner_and_name.pop(key, None)
    if tunnel.dedicated_subdomain:
        apps_by_dedicated_subdomain.pop(tunnel.dedicated_subdomain, None)
    owner = users_by_subdomain.get(tunnel.owner_subdomain)
    if owner is not None:
        owner.apps = [a for a in owner.apps if a.name != tunnel.name]
    for t in [t for t, k in app_tokens.items() if k == key]:
        app_tokens.pop(t, None)
    for conn_id, browser_ws in list(tunnel.browser_ws.items()):
        try:
            await browser_ws.close(code=4410)
        except Exception:
            pass
    tunnel.browser_ws.clear()
    if tunnel.ws is not None:
        try:
            await tunnel.ws.close(code=4410)
        except Exception:
            pass
    _delete_app_db(tunnel.owner_subdomain, tunnel.name)


async def _delete_user(user: User) -> None:
    for tunnel in list(user.apps):
        await _delete_app(tunnel)
    users_by_subdomain.pop(user.subdomain, None)
    users_by_api_key.pop(user.api_key, None)
    for sid in [s for s, (owner, _) in sessions.items() if owner == user.subdomain]:
        sessions.pop(sid, None)
    _delete_user_db(user.subdomain)


def register_routes(app: FastAPI) -> None:
    @app.get("/api/v1/health")
    def health() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @app.get("/_caddy_ask")
    def caddy_ask(domain: str) -> Response:
        """Caddy's on-demand TLS calls this before issuing a cert for a hostname
        it hasn't seen before -- without this check, anyone could make Caddy
        mint certs for arbitrary hostnames pointed at this server. Only needed
        if you're using Caddy's on-demand TLS in front of this; ignore it
        otherwise."""
        host = domain.split(":")[0].lower()
        if host == BASE_DOMAIN:
            return Response(status_code=200)
        subdomain = subdomain_from_host(host)
        if subdomain == CONTROL_SUBDOMAIN:
            return Response(status_code=200)
        if subdomain and (subdomain in users_by_subdomain or subdomain in apps_by_dedicated_subdomain):
            return Response(status_code=200)
        raise HTTPException(403, "unknown domain")

    @app.post("/api/v1/users/create", response_model=CreateUserResponse)
    def create_user(req: CreateUserRequest) -> CreateUserResponse:
        _require_admin(req.admin_secret)
        subdomain = _gen_unique_subdomain()
        api_key = secrets.token_urlsafe(24)
        user = User(subdomain=subdomain, api_key=api_key)
        users_by_subdomain[subdomain] = user
        users_by_api_key[api_key] = user
        _save_user(user)
        return CreateUserResponse(subdomain=subdomain, api_key=api_key)

    @app.post("/api/v1/apps/register", response_model=AppRegisterResponse)
    def register_app(req: AppRegisterRequest) -> AppRegisterResponse:
        _require_admin(req.admin_secret)
        user = users_by_subdomain.get(req.user_subdomain)
        if user is None:
            raise HTTPException(404, "unknown user_subdomain")
        return _create_app_for_user(user, req.app_name, req.entry_path, req.dedicated_subdomain, req.public)

    # ---- self-serve, X-Api-Key OR session-cookie scoped ------------------------
    @app.get("/api/v1/my/account", response_model=MyAccountInfo)
    def my_account(x_api_key: str | None = Header(default=None),
                   session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> MyAccountInfo:
        user = _user_from_request(x_api_key, session_id)
        return MyAccountInfo(subdomain=user.subdomain, api_key=user.api_key)

    @app.post("/api/v1/my/rotate_key", response_model=RotateKeyResponse)
    def rotate_my_key(x_api_key: str | None = Header(default=None),
                      session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> RotateKeyResponse:
        user = _user_from_request(x_api_key, session_id)
        users_by_api_key.pop(user.api_key, None)
        user.api_key = secrets.token_urlsafe(24)
        users_by_api_key[user.api_key] = user
        _save_user(user)
        return RotateKeyResponse(subdomain=user.subdomain, api_key=user.api_key)

    @app.get("/api/v1/my/apps", response_model=list[MyAppInfo])
    def list_my_apps(x_api_key: str | None = Header(default=None),
                     session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> list[MyAppInfo]:
        user = _user_from_request(x_api_key, session_id)
        return [MyAppInfo(name=a.name, entry_path=a.entry_path, connected=a.ws is not None,
                          dedicated_subdomain=a.dedicated_subdomain) for a in user.apps]

    @app.post("/api/v1/my/apps", response_model=AppRegisterResponse)
    def create_my_app(req: SelfAppRegisterRequest, x_api_key: str | None = Header(default=None),
                      session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> AppRegisterResponse:
        user = _user_from_request(x_api_key, session_id)
        # No is_owner/trust-tier gate on `public` here -- everyone with an account
        # on YOUR OWN relay server is trusted by definition, unlike a multi-tenant
        # hosted service where an untrusted stranger could self-serve-signup.
        return _create_app_for_user(user, req.name, req.entry_path, req.dedicated_subdomain, req.public)

    @app.delete("/api/v1/my/apps/{name}")
    async def delete_my_app(name: str, x_api_key: str | None = Header(default=None),
                            session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE)) -> dict:
        user = _user_from_request(x_api_key, session_id)
        tunnel = apps_by_owner_and_name.get((user.subdomain, name))
        if tunnel is None:
            raise HTTPException(404, "unknown app")
        await _delete_app(tunnel)
        return {"status": "deleted"}

    # ---- admin: bootstrap/manage accounts on your own relay --------------------
    @app.post("/api/v1/admin/users/list", response_model=list[AdminUserInfo])
    def admin_list_users(req: AdminActionRequest) -> list[AdminUserInfo]:
        _require_admin(req.admin_secret)
        return [
            AdminUserInfo(subdomain=u.subdomain, apps=[
                MyAppInfo(name=a.name, entry_path=a.entry_path, connected=a.ws is not None,
                         dedicated_subdomain=a.dedicated_subdomain) for a in u.apps
            ])
            for u in users_by_subdomain.values()
        ]

    @app.post("/api/v1/admin/users/{subdomain}/delete")
    async def admin_delete_user(subdomain: str, req: AdminActionRequest) -> dict:
        _require_admin(req.admin_secret)
        user = users_by_subdomain.get(subdomain)
        if user is None:
            raise HTTPException(404, "unknown user")
        await _delete_user(user)
        return {"status": "deleted"}

    @app.post("/api/v1/admin/users/{subdomain}/rotate_key", response_model=RotateKeyResponse)
    def admin_rotate_key(subdomain: str, req: AdminActionRequest) -> RotateKeyResponse:
        _require_admin(req.admin_secret)
        user = users_by_subdomain.get(subdomain)
        if user is None:
            raise HTTPException(404, "unknown user")
        users_by_api_key.pop(user.api_key, None)
        user.api_key = secrets.token_urlsafe(24)
        users_by_api_key[user.api_key] = user
        _save_user(user)
        sessions_to_clear = [s for s, (owner, _) in sessions.items() if owner == subdomain]
        for sid in sessions_to_clear:
            sessions.pop(sid, None)
        return RotateKeyResponse(subdomain=user.subdomain, api_key=user.api_key)

    @app.websocket("/api/v1/tunnel/ws")
    async def tunnel_ws(websocket: WebSocket, token: str) -> None:
        key = app_tokens.get(token)
        tunnel = apps_by_owner_and_name.get(key) if key else None
        if tunnel is None:
            await websocket.close(code=4401)
            return
        await websocket.accept()
        tunnel.ws = websocket
        try:
            while True:
                msg = await websocket.receive_json()
                msg_type = msg.get("type")
                if msg_type == "http.response":
                    fut = tunnel.pending.pop(msg.get("request_id"), None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
                elif msg_type == "ws.opened":
                    fut = tunnel.ws_open_pending.get(msg.get("conn_id"))
                    if fut is not None and not fut.done():
                        fut.set_result(True)
                elif msg_type == "ws.open_failed":
                    fut = tunnel.ws_open_pending.get(msg.get("conn_id"))
                    if fut is not None and not fut.done():
                        fut.set_result(False)
                elif msg_type == "ws.message":
                    browser_ws = tunnel.browser_ws.get(msg.get("conn_id"))
                    if browser_ws is not None:
                        data = base64.b64decode(msg.get("data_b64", ""))
                        try:
                            if msg.get("binary"):
                                await browser_ws.send_bytes(data)
                            else:
                                await browser_ws.send_text(data.decode("utf-8", errors="replace"))
                        except Exception:
                            pass
                elif msg_type == "ws.close":
                    browser_ws = tunnel.browser_ws.pop(msg.get("conn_id"), None)
                    if browser_ws is not None:
                        try:
                            await browser_ws.close(code=msg.get("code") or 1000)
                        except Exception:
                            pass
        except WebSocketDisconnect:
            pass
        finally:
            if tunnel.ws is websocket:
                tunnel.ws = None
                for fut in tunnel.pending.values():
                    if not fut.done():
                        fut.set_exception(RuntimeError("tunnel agent disconnected"))
                tunnel.pending.clear()
                for fut in tunnel.ws_open_pending.values():
                    if not fut.done():
                        fut.set_result(False)
                tunnel.ws_open_pending.clear()
                for conn_id, browser_ws in list(tunnel.browser_ws.items()):
                    try:
                        await browser_ws.close(code=4502)
                    except Exception:
                        pass
                tunnel.browser_ws.clear()


_LOGIN_FORM = """<!doctype html><html><body style="font-family:sans-serif;max-width:320px;margin:15vh auto">
<form method="post" action="/_gateway_login"><h3>Sign in</h3>
<input type="password" name="api_key" placeholder="API key" autofocus
 style="width:100%;padding:8px;font-size:1rem">
<button type="submit" style="width:100%;padding:8px;margin-top:8px">Enter</button>
{error}</form></body></html>"""

_DASHBOARD = """<!doctype html><html><body style="font-family:sans-serif;max-width:480px;margin:15vh auto">
<h3>Your apps</h3>{apps}</body></html>"""

_HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "upgrade", "content-length", "host"}


def _authed_owner_from_session_cookie(session_id: str | None) -> str | None:
    entry = sessions.get(session_id) if session_id else None
    if entry is None:
        return None
    owner, expires_at = entry
    return owner if expires_at > time.time() else None


def _app_url(user: User, a: AppTunnel) -> str:
    if a.dedicated_subdomain:
        return f"https://{a.dedicated_subdomain}.{BASE_DOMAIN}{a.entry_path}"
    return f"https://{user.subdomain}.{BASE_DOMAIN}/{a.name}{a.entry_path}"


def _resolve_gateway_target(host: str, path: str) -> tuple[User, AppTunnel, str] | None:
    """(host, path) -> (owning user, the app, forward_path with any owner-prefix
    already stripped) for a request aimed at a tunneled app -- either the normal
    path-based route (https://<user>.<domain>/<app-name>/...) or an app's own
    dedicated subdomain. Shared by the HTTP routing branch and the WS relay
    middleware below, so both stay in sync automatically."""
    subdomain = subdomain_from_host(host)
    if subdomain is None:
        return None
    dedicated_tunnel = apps_by_dedicated_subdomain.get(subdomain)
    if dedicated_tunnel is not None:
        owner = users_by_subdomain.get(dedicated_tunnel.owner_subdomain)
        if owner is None:
            return None
        return owner, dedicated_tunnel, path
    user = users_by_subdomain.get(subdomain)
    if user is None or path == "/":
        return None
    first, _, rest = path[1:].partition("/")
    app_tunnel = next((a for a in user.apps if a.name == first), None)
    if app_tunnel is None:
        return None
    return user, app_tunnel, "/" + rest


WS_OPEN_TIMEOUT_SECONDS = 10
# Headers that belong to the BROWSER's handshake with THIS relay, not the
# tunnel_agent's own separate handshake with the local app -- forwarding
# sec-websocket-key/version verbatim makes the agent's own websockets.connect()
# send a stale/mismatched key on its own brand new handshake, which the local
# app correctly rejects (HTTP 400). Each hop gets its own real WS handshake.
_WS_HANDSHAKE_ONLY_HEADERS = _HOP_BY_HOP | {
    "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
    "sec-websocket-protocol", "sec-websocket-accept", "origin",
}


class TunnelWebSocketMiddleware:
    """Raw ASGI middleware -- NOT BaseHTTPMiddleware, which never sees a
    WebSocket-upgrade scope at all. This is what lets a browser open a real
    WebSocket THROUGH the tunnel to a tunneled app (e.g. a live-chat socket)
    instead of only ever supporting buffered request/response. Anything not
    aimed at a registered tunnel host/app -- including this server's own
    /api/v1/tunnel/ws control socket -- falls straight through unaffected."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "websocket":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        host = headers.get(b"host", b"").decode("latin-1")
        resolved = _resolve_gateway_target(host, scope["path"])
        if resolved is None:
            await self.app(scope, receive, send)
            return
        user, app_tunnel, forward_path = resolved
        websocket = WebSocket(scope, receive=receive, send=send)
        if not app_tunnel.public:
            owner = _authed_owner_from_session_cookie(websocket.cookies.get(SESSION_COOKIE))
            if owner != user.subdomain:
                await websocket.close(code=4401)
                return
        await _relay_browser_ws(websocket, app_tunnel, forward_path)


async def _relay_browser_ws(websocket: WebSocket, tunnel: AppTunnel, forward_path: str) -> None:
    """Accepts the browser's WS upgrade, asks the tunnel_agent to open a real WS
    connection to the local app (ws.open), and once confirmed (ws.opened),
    relays frames bidirectionally: this coroutine reads from the browser and
    forwards ws.message upstream; the control socket's own receive loop
    (tunnel_ws) routes incoming ws.message/ws.close frames from the agent back
    to tunnel.browser_ws[conn_id]."""
    if tunnel.ws is None:
        await websocket.close(code=4502)
        return
    await websocket.accept()
    conn_id = secrets.token_hex(8)
    opened: asyncio.Future = asyncio.get_event_loop().create_future()
    tunnel.ws_open_pending[conn_id] = opened
    tunnel.browser_ws[conn_id] = websocket
    query = f"?{websocket.url.query}" if websocket.url.query else ""
    try:
        async with tunnel.send_lock:
            await tunnel.ws.send_json({
                "type": "ws.open", "conn_id": conn_id, "path": forward_path + query,
                "headers": {k: v for k, v in websocket.headers.items()
                           if k.lower() not in _WS_HANDSHAKE_ONLY_HEADERS},
            })
    except Exception:
        tunnel.ws_open_pending.pop(conn_id, None)
        tunnel.browser_ws.pop(conn_id, None)
        await websocket.close(code=4502)
        return

    try:
        ok = await asyncio.wait_for(opened, timeout=WS_OPEN_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        ok = False
    finally:
        tunnel.ws_open_pending.pop(conn_id, None)
    if not ok:
        tunnel.browser_ws.pop(conn_id, None)
        await websocket.close(code=4502)
        return

    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text")
            raw_bytes = message.get("bytes")
            if text is not None:
                raw = text.encode()
                data_b64, binary = base64.b64encode(raw).decode("ascii"), False
            elif raw_bytes is not None:
                raw = raw_bytes
                data_b64, binary = base64.b64encode(raw).decode("ascii"), True
            else:
                continue
            try:
                async with tunnel.send_lock:
                    await tunnel.ws.send_json({"type": "ws.message", "conn_id": conn_id,
                                              "data_b64": data_b64, "binary": binary})
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    finally:
        tunnel.browser_ws.pop(conn_id, None)
        if tunnel.ws is not None:
            try:
                async with tunnel.send_lock:
                    await tunnel.ws.send_json({"type": "ws.close", "conn_id": conn_id, "code": 1000})
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass


class TunnelMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        host = request.headers.get("host", "")
        subdomain = subdomain_from_host(host)
        if subdomain in apps_by_dedicated_subdomain:
            return await self._handle_dedicated(request, apps_by_dedicated_subdomain[subdomain])
        if subdomain in users_by_subdomain:
            return await self._handle_gateway(request, users_by_subdomain[subdomain])
        return await call_next(request)  # bare domain / control subdomain -- normal routing

    def _authed_owner(self, request: Request) -> str | None:
        return _authed_owner_from_session_cookie(request.cookies.get(SESSION_COOKIE))

    async def _handle_dedicated(self, request: Request, tunnel: AppTunnel) -> Response:
        if tunnel.public:
            return await self._handle_app(request, tunnel, request.url.path)
        owner = self._authed_owner(request)
        if owner != tunnel.owner_subdomain:
            login_url = f"https://{tunnel.owner_subdomain}.{BASE_DOMAIN}/"
            return HTMLResponse(
                f'<p>Please <a href="{login_url}">sign in</a> at your gateway first, then reload this page.</p>',
                status_code=401)
        return await self._handle_app(request, tunnel, request.url.path)

    async def _handle_gateway(self, request: Request, user: User) -> Response:
        owner = self._authed_owner(request)
        if owner != user.subdomain:
            if request.method == "POST" and request.url.path == "/_gateway_login":
                form = await request.form()
                if secrets.compare_digest(str(form.get("api_key", "")), user.api_key):
                    sid = secrets.token_urlsafe(24)
                    sessions[sid] = (user.subdomain, time.time() + SESSION_TTL_SECONDS)
                    resp = HTMLResponse("<script>location.href='/'</script>")
                    resp.set_cookie(SESSION_COOKIE, sid, httponly=True, max_age=SESSION_TTL_SECONDS,
                                    samesite="lax", domain=f".{BASE_DOMAIN}", secure=SESSION_COOKIE_SECURE)
                    return resp
                return HTMLResponse(_LOGIN_FORM.format(error="<p style='color:red'>wrong key</p>"), status_code=401)
            return HTMLResponse(_LOGIN_FORM.format(error=""), status_code=401)

        path = request.url.path
        if path != "/":
            resolved = _resolve_gateway_target(request.headers.get("host", ""), path)
            if resolved is not None:
                _, tunnel, forward_path = resolved
                return await self._handle_app(request, tunnel, forward_path)

        if not user.apps:
            items = "<p style='color:#888'>No apps registered yet.</p>"
        else:
            items = "".join(f'<p><a href="{_app_url(user, a)}">{a.name}</a></p>' for a in user.apps)
        return HTMLResponse(_DASHBOARD.format(apps=items))

    async def _handle_app(self, request: Request, tunnel: AppTunnel, forward_path: str) -> Response:
        if tunnel.ws is None:
            return Response("tunnel agent offline", status_code=502)

        body = await request.body()
        request_id = secrets.token_hex(8)
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        tunnel.pending[request_id] = fut

        query = f"?{request.url.query}" if request.url.query else ""
        payload = {
            "type": "http.request",
            "request_id": request_id,
            "method": request.method,
            "path": forward_path + query,
            "headers": {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP},
            "body_b64": base64.b64encode(body).decode("ascii"),
        }
        try:
            async with tunnel.send_lock:
                await tunnel.ws.send_json(payload)
        except Exception:
            tunnel.pending.pop(request_id, None)
            return Response("tunnel agent connection lost", status_code=502)

        try:
            result = await asyncio.wait_for(fut, timeout=REQUEST_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            tunnel.pending.pop(request_id, None)
            return Response("tunnel agent timed out", status_code=504)
        except RuntimeError as e:
            return Response(str(e), status_code=502)

        resp_headers = dict(result.get("headers", {}))
        for h in _HOP_BY_HOP:
            resp_headers.pop(h, None)
        response_body = base64.b64decode(result.get("body_b64", ""))
        return Response(content=response_body, status_code=result.get("status", 502), headers=resp_headers)


app = FastAPI(title="NovaRelynx relay (self-host)")
register_routes(app)
# Must be added AFTER register_routes so /api/v1/* still match normal FastAPI
# routing on the control-plane host; this middleware only intercepts requests
# whose Host header is a registered tunnel subdomain.
app.add_middleware(TunnelMiddleware)
app.add_middleware(TunnelWebSocketMiddleware)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
