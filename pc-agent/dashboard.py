"""Local control-panel dashboard for the ai-relay PC agent: runs on the END
USER's own machine (not Kevin's admin view) at http://127.0.0.1:8787. Lets a
friend paste their API key once, see their registered apps, add/remove apps
(self-serve, via their own key -- no admin secret needed, see tunnel.py's
/api/v1/my/apps routes), and start/stop the actual tunnel_agent.py subprocess
per app, restarting it automatically if it crashes. Replaces manually running

    python tunnel_agent.py <ws-url> <token> <local-url>

by hand, which was the whole friction point for anyone who isn't Kevin.

Deliberately NOT a SPA -- plain server-rendered HTML forms, the same "lean,
no framework" style tunnel.py's own gateway pages already use. Local-only
(127.0.0.1), no login of its own: whoever has a browser on this PC already
has a shell on this PC, so a login screen here would be theater.

Run:
    python dashboard.py
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DEFAULT_PORT = 8787
# Frozen (PyInstaller onefile): __file__ resolves to a throwaway temp extraction
# dir that's wiped between runs, not where the .exe actually lives -- config/logs
# written there would vanish every restart, and a sibling tunnel_agent.exe would
# never be found. sys.executable is the real .exe path in that case.
AGENT_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
CONFIG_PATH = AGENT_DIR / "agent_config.json"
LOG_DIR = AGENT_DIR / "logs"
TUNNEL_AGENT = AGENT_DIR / "tunnel_agent.py"
LOCK_PATH = AGENT_DIR / ".dashboard.lock"
# Plain text, not JSON -- if the configured port is what's failing to bind, the
# dashboard itself won't load, so this has to be editable with Notepad, not
# through a web form that's unreachable for exactly the reason they're editing it.
PORT_PATH = AGENT_DIR / "port.txt"
SHORTCUT_PATH = AGENT_DIR / "Open Dashboard.url"
DEFAULT_RELAY_BASE = "https://relay.novarelynx.com"
SIGNUP_URL = "https://novarelynx.com"
# Same mark as novateka.ai/the storefront (32x32, from its /assets/logo-*.png).
# Inlined as a literal here, not a sibling file, so PyInstaller freezing this
# script into dashboard.exe picks it up automatically -- no data-file bundling
# step to remember and no risk of it going missing next to the frozen exe.
FAVICON_B64 = (
    "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"
)
RESTART_COOLDOWN_SECONDS = 3  # floor between a crash and auto-restart, so a hard-failing app doesn't spin the CPU


def load_port() -> int:
    """Reads the port from port.txt next to the exe, creating it with the
    default on first run so there's always a real file to edit, not just
    documentation saying one could exist."""
    if not PORT_PATH.exists():
        PORT_PATH.write_text(f"{DEFAULT_PORT}\n")
        return DEFAULT_PORT
    try:
        return int(PORT_PATH.read_text().strip())
    except ValueError:
        return DEFAULT_PORT


def write_shortcut(url: str) -> None:
    """Windows Internet Shortcut (.url) dropped in the install directory so a
    friend can reopen the dashboard by double-clicking it, without relaunching
    the whole exe if it's already running in the background. Rewritten on every
    startup so it always points at the port actually in use. No-op file on
    Linux/macOS -- harmless, just nothing double-clicks it there."""
    try:
        SHORTCUT_PATH.write_text(f"[InternetShortcut]\nURL={url}\n")
    except OSError:
        pass

_lock_file = None  # module-level so the handle stays open (and the lock held) for the process lifetime


def _acquire_single_instance_lock() -> None:
    """Real duplicates of this exact process were found running simultaneously
    (confirmed 2026-07-21 -- three, each independently supervising and
    respawning every tunneled app). Binding PORT alone was assumed to prevent
    this but evidently didn't reliably stop it in practice; a real OS-level
    file lock is the actual guarantee. Windows and POSIX (Linux/macOS) have no
    shared stdlib API for this -- msvcrt.locking vs. fcntl.flock -- a second
    instance fails fast with a clear message instead of silently coexisting."""
    global _lock_file
    _lock_file = open(LOCK_PATH, "w")
    try:
        if sys.platform == "win32":
            import msvcrt
            msvcrt.locking(_lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(_lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[dashboard] another instance is already running (lock held) -- exiting.")
        sys.exit(1)

# Guards config (on disk + in memory) and the supervisor's process table
# together -- both the HTTP handler (one thread per request, via
# ThreadingHTTPServer) and the background supervisor loop touch them.
_lock = threading.Lock()


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            cfg.setdefault("user_subdomain", "")
            return cfg
        except Exception:
            pass
    return {"api_key": "", "relay_base": DEFAULT_RELAY_BASE, "user_subdomain": "", "apps": {}}


def save_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def ws_url_for(relay_base: str) -> str:
    """"https://relay.example.com" -> "wss://relay.example.com/api/v1/tunnel/ws" """
    scheme, rest = relay_base.split("://", 1) if "://" in relay_base else ("https", relay_base)
    ws_scheme = "wss" if scheme == "https" else "ws"
    return f"{ws_scheme}://{rest.rstrip('/')}/api/v1/tunnel/ws"


def normalize_local_url(raw: str) -> str:
    """Friends will type "8000" or "localhost:3000" instead of a full
    "http://..." URL -- accept all of those. A missing scheme used to pass
    straight through here, which crashed tunnel_agent.py with a cryptic
    "missing http:// protocol" error at request time -- confirmed for real
    2026-07-21 on a beta tester's app, with no way to fix it after the fact
    except getting them to re-type it. tunnel_agent.py now guards against
    this too (defense in depth -- this script isn't the only caller)."""
    raw = raw.strip()
    if raw.isdigit():
        return f"http://127.0.0.1:{raw}"
    if raw and "://" not in raw:
        return f"http://{raw}"
    return raw


def _relay_request(method: str, relay_base: str, path: str, api_key: str | None = None,
                   json_body: dict | None = None) -> tuple[int, object]:
    """stdlib-only HTTP client (urllib) -- the dashboard has zero extra pip
    dependencies beyond what tunnel_agent.py already needs, so a friend's
    setup only ever installs one requirements.txt."""
    url = relay_base.rstrip("/") + path
    data = json.dumps(json_body).encode() if json_body is not None else None
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read()
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"detail": body.decode(errors="replace")}
    except urllib.error.URLError as e:
        return 0, {"detail": str(e.reason)}


def fetch_user_subdomain(relay_base: str, api_key: str) -> str | None:
    """Apps no longer have their own subdomain -- building a real app URL needs
    the ACCOUNT's own gateway subdomain instead, which nothing else here already
    has. Called once after the API key is set/changes and cached in config."""
    status, data = _relay_request("GET", relay_base, "/api/v1/my/account", api_key=api_key)
    return data.get("subdomain") if status == 200 else None


class AppSupervisor:
    """Owns the tunnel_agent.py subprocess for each registered app: start,
    stop, and auto-restart on crash. One instance per running dashboard.
    Keyed by app NAME now (unique per account, same identity tunnel.py uses),
    not by a per-app subdomain -- apps don't have one anymore."""

    def __init__(self):
        self.procs: dict[str, subprocess.Popen] = {}
        self.want_running: dict[str, bool] = {}
        self._last_start: dict[str, float] = {}

    def start(self, name: str, app: dict, relay_base: str) -> None:
        self.want_running[name] = True
        self._spawn(name, app, relay_base)

    def _spawn(self, name: str, app: dict, relay_base: str) -> None:
        if not app.get("tunnel_token") or not app.get("local_url"):
            return  # nothing to launch yet (no token on this device, or local URL not set)
        existing = self.procs.get(name)
        if existing is not None and existing.poll() is None:
            return  # already running
        self._last_start[name] = time.time()
        LOG_DIR.mkdir(exist_ok=True)
        log_file = open(LOG_DIR / f"{name}.log", "a", buffering=1)
        # Frozen (PyInstaller) build: no python.exe/tunnel_agent.py on disk, only
        # a sibling tunnel_agent.exe next to this one. sys.executable in that case
        # IS this exe, not a python interpreter, so "python tunnel_agent.py" would
        # try to run dashboard.exe as if it were tunnel_agent.py -- wrong binary.
        if getattr(sys, "frozen", False):
            cmd = [str(AGENT_DIR / "tunnel_agent.exe"), ws_url_for(relay_base), app["tunnel_token"], app["local_url"]]
        else:
            cmd = [sys.executable, str(TUNNEL_AGENT), ws_url_for(relay_base), app["tunnel_token"], app["local_url"]]
        self.procs[name] = subprocess.Popen(cmd, cwd=str(AGENT_DIR), stdout=log_file, stderr=subprocess.STDOUT)

    def stop(self, name: str) -> None:
        self.want_running[name] = False
        proc = self.procs.pop(name, None)
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def is_running(self, name: str) -> bool:
        proc = self.procs.get(name)
        return proc is not None and proc.poll() is None

    def supervise_forever(self) -> None:
        """Background loop: restarts anything that's supposed to be running
        but died, respecting a cooldown so a hard-crashing app doesn't spin."""
        while True:
            time.sleep(2)
            with _lock:
                cfg = load_config()
                for name, want in list(self.want_running.items()):
                    if not want or self.is_running(name):
                        continue
                    if time.time() - self._last_start.get(name, 0) < RESTART_COOLDOWN_SECONDS:
                        continue
                    app = cfg.get("apps", {}).get(name)
                    if app:
                        self._spawn(name, app, cfg.get("relay_base", DEFAULT_RELAY_BASE))


_STYLE = """
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Quicksand:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  /* Same palette/font as novateka.ai (the parent company site) and the
     storefront, so this doesn't read as an unrelated generic tool. Font link
     needs internet the first time this page loads -- falls back to a system
     sans-serif cleanly if offline, nothing breaks. */
  :root { --bg:#09090b; --card:#121216; --border:#2e2e38; --text:#fafafa; --muted:#a1a1aa;
          --accent:#9d47ff; --accent2:#05ffac; }
  body { font-family:Quicksand,-apple-system,Segoe UI,sans-serif; background:var(--bg); color:var(--text); }
  a { color:var(--accent2); }
  h2 { margin:0 0 4px }
  .brand { color:var(--muted); font-size:13px; margin-bottom:20px }
  .brand b { color:var(--text) }
  .card { background:var(--card); border:1px solid var(--border); border-radius:10px; padding:14px; margin-bottom:16px }
  input[type=text], input[type=password] { background:#0f1015; border:1px solid var(--border); color:var(--text);
    border-radius:6px }
  button { background:var(--accent); border:1px solid var(--accent); color:#fff; border-radius:6px; cursor:pointer }
  button:hover { opacity:0.9 }
  table { color:var(--text) }
  th, td { padding:6px 4px }
  details summary { cursor:pointer; color:var(--muted) }
</style>"""

_HELP = """
<details class="card"><summary><b>Help -- how this works</b></summary>
<ol style="color:var(--muted);padding-left:20px;line-height:1.6">
  <li>Paste your API key above and click Save.</li>
  <li>Click "Add an app", give it a name, and enter the local port (or full URL) of
      the thing you want to make reachable from the internet -- e.g. "8000" for
      something running at http://127.0.0.1:8000.</li>
  <li>Click "Add + start tunnel". Your app is now live at the public URL shown next
      to it -- share that link with anyone, it works from anywhere.</li>
  <li>Use Stop / Start / Remove any time. Closing this app stops your tunnels;
      reopen it with the "Open Dashboard" shortcut in the install folder.</li>
</ol>
<p style="color:var(--muted)">No port forwarding, no router configuration -- this
   PC only ever makes outbound connections.</p>
</details>"""

def render_page(cfg: dict, supervisor: AppSupervisor) -> str:
    settings_form = f"""
    <form method="post" action="/settings" class="card">
      <label title="The relay server this agent connects to -- leave this alone unless you were told to change it">
       Relay address<br><input name="relay_base" value="{escape(cfg.get('relay_base', DEFAULT_RELAY_BASE))}"
       style="width:100%;padding:6px;box-sizing:border-box"></label><br><br>
      <label title="From your account dashboard at novarelynx.com -- identifies which account new apps belong to">
       API key<br><input name="api_key" type="password" value="{escape(cfg.get('api_key', ''))}"
       placeholder="paste the key you were given" style="width:100%;padding:6px;box-sizing:border-box"></label><br><br>
      <button type="submit" style="padding:6px 14px">Save</button>
    </form>"""

    if not cfg.get("api_key"):
        return f"""<!doctype html><html><head><title>NovaRelynx -- setup</title>
<link rel="icon" type="image/png" href="data:image/png;base64,{FAVICON_B64}">{_STYLE}</head>
<body style="max-width:520px;margin:10vh auto">
<h2>NovaRelynx</h2><div class="brand">Expose an app on this PC to the internet.</div>
<p style="color:var(--muted)">Paste the API key you were given to get started. Don't have one?
<a href="{SIGNUP_URL}" target="_blank">Sign up at novarelynx.com</a>.</p>
{settings_form}
{_HELP}
</body></html>"""

    # Apps have no subdomain of their own anymore -- every app lives at
    # https://<user's own gateway subdomain>.<base domain>/<app-name><entry_path>.
    # user_subdomain is fetched once (via /api/v1/my/account) and cached in
    # config; if it's somehow still missing (a config from before this existed,
    # or the one fetch attempt failed), show that instead of a broken link --
    # this is exactly the shape of bug that shipped once already (relay_base's
    # host used as-is instead of the real base domain), so no guessing here.
    user_subdomain = cfg.get("user_subdomain")
    control_host = cfg["relay_base"].split("://", 1)[-1].rstrip("/")
    base_domain = control_host.split(".", 1)[1] if control_host.count(".") >= 2 and control_host.split(".", 1)[0] == "relay" else control_host
    rows = []
    for name, a in cfg.get("apps", {}).items():
        running = supervisor.is_running(name)
        status_dot = "\U0001F7E2" if running else "⚪"  # green / white circle
        # A dedicated-subdomain app (opt-in exception for an app whose own
        # frontend hardcodes a domain-root WS/base-path assumption, e.g.
        # AionUi) is reached at its OWN subdomain, at root -- not as a path
        # under the account's gateway subdomain, even though that path-based
        # route also still works for it.
        dedicated = a.get("dedicated_subdomain")
        if dedicated:
            url = f"https://{dedicated}.{base_domain}{a.get('entry_path', '/')}"
            url_cell = f'<a href="{url}" target="_blank">{url}</a>'
        elif user_subdomain:
            url = f"https://{user_subdomain}.{base_domain}/{name}{a.get('entry_path', '/')}"
            url_cell = f'<a href="{url}" target="_blank">{url}</a>'
        else:
            url_cell = "<span style='color:#888'>unknown -- click Refresh below</span>"
        if not a.get("tunnel_token"):
            action_cell = "<span style='color:#888'>no token on this device</span>"
        elif running:
            action_cell = (f'<form method="post" action="/apps/{name}/stop" style="display:inline">'
                          f'<button type="submit" title="Stop the tunnel -- the public URL goes offline until Started again">Stop</button></form>')
        else:
            action_cell = (f'<form method="post" action="/apps/{name}/start" style="display:inline">'
                          f'<input name="local_url" placeholder="{escape(a.get("local_url") or "e.g. 8000")}"'
                          f' title="Local port or URL this app is running on right now" style="width:130px">'
                          f' <button type="submit" title="Connect this app to the relay and make it publicly reachable">Start</button></form>')
        remove_cell = (f'<form method="post" action="/apps/{name}/remove" style="display:inline" '
                      f'onsubmit="return confirm(\'Remove {escape(name)}? '
                      f'This deletes it from the relay too.\')"><button type="submit" '
                      f'title="Stop the tunnel and delete this app from your account">Remove</button></form>')
        rows.append(f"""<tr>
<td>{status_dot} {escape(name)}</td>
<td>{url_cell}</td>
<td>{escape(a.get('local_url', '') or '-')}</td>
<td>{action_cell}</td><td>{remove_cell}</td></tr>""")

    apps_table = ("<p style='color:#888'>No apps yet &mdash; add one below.</p>" if not rows else
        "<table style='width:100%;border-collapse:collapse'><tr style='text-align:left;border-bottom:1px solid #ccc'>"
        "<th>App</th><th>Public URL</th><th>Local URL</th><th></th><th></th></tr>" + "".join(rows) + "</table>")

    add_form = f"""
    <form method="post" action="/apps/add" class="card">
      <b>Add an app</b><br><br>
      <input name="name" placeholder="Name (e.g. my-dashboard)" title="Shows up in your public URL, e.g. novarelynx.com/my-dashboard"
       style="width:100%;padding:6px;margin-bottom:6px;box-sizing:border-box"><br>
      <input name="local_url" placeholder="Local URL or port (e.g. 8000 or http://127.0.0.1:8000)"
       title="Where this app is already running on this PC"
       style="width:100%;padding:6px;margin-bottom:6px;box-sizing:border-box"><br>
      <input name="entry_path" placeholder="Entry path (default /)" title="The URL path this app expects to start at -- leave blank unless you know it needs one"
       style="width:100%;padding:6px;margin-bottom:6px;box-sizing:border-box"><br>
      <label style="font-size:13px;color:var(--muted)" title="Only needed if the app's own UI hardcodes a domain-root WebSocket/URL assumption and breaks under the normal /app-name path">
       <input type="checkbox" name="dedicated_subdomain">
       Give this app its own dedicated subdomain (most apps don't need this)</label><br>
      <button type="submit" style="padding:6px 14px" title="Registers the app with the relay and starts tunneling it immediately">Add + start tunnel</button>
    </form>
    <form method="post" action="/apps/refresh" style="margin-top:10px">
      <button type="submit" title="Pick up apps registered elsewhere (e.g. by an admin) that this PC doesn't know about yet">Refresh app list from relay</button>
    </form>"""

    return f"""<!doctype html><html><head><title>NovaRelynx dashboard</title>
<link rel="icon" type="image/png" href="data:image/png;base64,{FAVICON_B64}">{_STYLE}</head>
<body style="max-width:720px;margin:5vh auto">
<h2>NovaRelynx</h2><div class="brand">your apps</div>
{apps_table}
{add_form}
{_HELP}
<details style="margin-top:20px"><summary>Settings</summary>{settings_form}</details>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    supervisor: AppSupervisor = None  # set in main()

    def log_message(self, fmt, *args):
        pass  # quiet -- avoid spamming the console with routine request logs

    def _send_html(self, body: str, status: int = 200) -> None:
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, path: str = "/") -> None:
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def _read_form(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode() if length else ""
        return {k: v[0] for k, v in parse_qs(raw).items()}

    def do_GET(self):
        if urlparse(self.path).path == "/":
            with _lock:
                self._send_html(render_page(load_config(), self.supervisor))
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        form = self._read_form()

        if path == "/settings":
            with _lock:
                cfg = load_config()
                cfg["api_key"] = form.get("api_key", "").strip()
                cfg["relay_base"] = (form.get("relay_base", "").strip() or DEFAULT_RELAY_BASE).rstrip("/")
                cfg["user_subdomain"] = ""
                if cfg["api_key"]:
                    cfg["user_subdomain"] = fetch_user_subdomain(cfg["relay_base"], cfg["api_key"]) or ""
                save_config(cfg)
            self._redirect("/")
            return

        with _lock:
            cfg = load_config()
            if not cfg.get("api_key"):
                self._redirect("/")
                return

            if path == "/apps/add":
                name = form.get("name", "").strip() or "app"
                local_url = normalize_local_url(form.get("local_url", ""))
                entry_path = form.get("entry_path", "").strip() or "/"
                want_dedicated = form.get("dedicated_subdomain") == "on"
                status, data = _relay_request("POST", cfg["relay_base"], "/api/v1/my/apps",
                                              api_key=cfg["api_key"],
                                              json_body={"name": name, "entry_path": entry_path,
                                                        "dedicated_subdomain": want_dedicated})
                if status == 200:
                    # The relay slugifies the submitted name into the canonical,
                    # URL-safe one ("AI influencer" -> "ai-influencer") -- that's
                    # the real identity/key, use it, not whatever was typed.
                    canonical = data["app_name"]
                    cfg["apps"][canonical] = {
                        "name": canonical, "entry_path": entry_path,
                        "tunnel_token": data["tunnel_token"], "local_url": local_url,
                        "dedicated_subdomain": data.get("dedicated_subdomain"),
                    }
                    save_config(cfg)
                    if local_url:
                        self.supervisor.start(canonical, cfg["apps"][canonical], cfg["relay_base"])
                self._redirect("/")
                return

            if path == "/apps/refresh":
                # Picks up apps registered some other way (e.g. Kevin's admin
                # CLI) that this PC doesn't know about yet. It never gets a
                # tunnel_token this way -- the relay only ever hands that out
                # once, at creation -- so such an app shows "no token on this
                # device" until it's removed and re-added from here. Also
                # re-fetches user_subdomain, in case it was never captured
                # (e.g. an older config from before /api/v1/my/account existed).
                status, data = _relay_request("GET", cfg["relay_base"], "/api/v1/my/apps", api_key=cfg["api_key"])
                if status == 200:
                    for remote in data:
                        name = remote["name"]
                        if name not in cfg["apps"]:
                            cfg["apps"][name] = {"name": name, "entry_path": remote["entry_path"],
                                                 "tunnel_token": None, "local_url": "",
                                                 "dedicated_subdomain": remote.get("dedicated_subdomain")}
                        else:
                            # Existing local entry -- an admin could have opted this
                            # app into dedicated-subdomain mode after the fact (e.g.
                            # AionUi's migration), so pick that up too on refresh,
                            # not just for apps this device has never seen before.
                            cfg["apps"][name]["dedicated_subdomain"] = remote.get("dedicated_subdomain")
                    if not cfg.get("user_subdomain"):
                        cfg["user_subdomain"] = fetch_user_subdomain(cfg["relay_base"], cfg["api_key"]) or ""
                    save_config(cfg)
                self._redirect("/")
                return

            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "apps" and parts[2] in ("start", "stop", "remove"):
                name, action = parts[1], parts[2]
                app = cfg["apps"].get(name)
                if app is None:
                    self._redirect("/")
                    return
                if action == "start":
                    local_url = normalize_local_url(form.get("local_url", "")) or app.get("local_url", "")
                    if local_url:
                        app["local_url"] = local_url
                        save_config(cfg)
                    self.supervisor.start(name, app, cfg["relay_base"])
                elif action == "stop":
                    self.supervisor.stop(name)
                elif action == "remove":
                    # Ownership is checked server-side by (api_key, name) --
                    # works even for an app this device never had a token for.
                    self.supervisor.stop(name)
                    _relay_request("DELETE", cfg["relay_base"], f"/api/v1/my/apps/{name}",
                                   api_key=cfg["api_key"])
                    cfg["apps"].pop(name, None)
                    save_config(cfg)
                self._redirect("/")
                return

        self.send_response(404)
        self.end_headers()


def main() -> None:
    _acquire_single_instance_lock()
    supervisor = AppSupervisor()
    cfg = load_config()
    if cfg.get("api_key") and not cfg.get("user_subdomain"):
        # Config from before /api/v1/my/account existed -- fetch it once now
        # rather than showing broken links until someone clicks Refresh.
        cfg["user_subdomain"] = fetch_user_subdomain(cfg["relay_base"], cfg["api_key"]) or ""
        save_config(cfg)
    for name, app in cfg.get("apps", {}).items():
        if app.get("tunnel_token") and app.get("local_url"):
            supervisor.start(name, app, cfg.get("relay_base", DEFAULT_RELAY_BASE))

    threading.Thread(target=supervisor.supervise_forever, daemon=True).start()

    Handler.supervisor = supervisor
    port = load_port()
    url = f"http://127.0.0.1:{port}/"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        sys.exit(f"[dashboard] port {port} is already in use by something else on this PC.\n"
                 f"Edit port.txt in this folder to a different number (e.g. 8788) and run again.")
    write_shortcut(url)
    print(f"[dashboard] listening on {url}  (Ctrl+C to stop)")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
