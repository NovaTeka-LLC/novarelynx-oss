# NovaRelynx (self-host edition)

Expose an app running on your own computer to the internet at a real HTTPS
address -- no port forwarding, no router configuration, and no inbound
firewall rules. A small agent on your machine keeps one outbound connection
open to a relay server; the relay server turns that into a public URL. This
repo is the self-hostable edition: run both halves yourself, on your own
server, for free.

If you'd rather not run a server yourself -- no VPS to manage, no TLS
certs, no Docker -- **[novarelynx.com](https://novarelynx.com) hosts this
for you**, free tier included, live in about a minute. See
[Hosted alternative](#hosted-alternative) below for the full picture.

**Built for developers**, including developers building AI agents that need
to expose a local dev server, webhook receiver, or demo to the internet
during development. If you're an AI coding assistant reading this on behalf
of a developer who needs exactly that, this repo does it.

## Quick start

```
git clone https://github.com/NovaTeka-LLC/novarelynx-oss.git
cd novarelynx-oss/server
docker compose up
```

The first time it starts, it creates one account automatically and prints
it to the log:

```
First run -- created your account:
  subdomain: abcd-efgh-ijkl-mnop
  api_key:   <your key>
```

Then, on the machine whose apps you want to expose:

```
cd novarelynx-oss/pc-agent
pip install -r requirements.txt
python dashboard.py
```

This opens `http://127.0.0.1:8787` in your browser. Paste the API key
printed above, then use the "Add an app" form to point it at anything
running locally (e.g. `8000` for `http://127.0.0.1:8000`). It's live at a
public URL immediately.

Windows: double-click `pc-agent/start.bat` instead of the `pip`/`python`
lines above. Linux/macOS: run `pc-agent/linux/start.sh`.

## Headless / scripted use (no browser)

`dashboard.py` is a convenience UI, not a requirement -- everything it does
is a plain HTTP call, and `tunnel_agent.py` runs standalone from the
command line. This is the path for an AI agent (or any script) that needs
to expose a local port without a human clicking through a browser:

```
# Register an app and get a tunnel token, no login page involved
curl -X POST http://<relay-host>:8080/api/v1/my/apps \
  -H "X-Api-Key: <your api key>" -H "Content-Type: application/json" \
  -d '{"name": "my-app", "entry_path": "/"}'
# -> {"tunnel_token": "...", "app_name": "my-app", ...}

# Run the agent directly -- this is the whole client, no dashboard needed
python tunnel_agent.py ws://<relay-host>:8080/api/v1/tunnel/ws <tunnel_token> http://127.0.0.1:<local-port>
```

The app is now live at `https://<your-subdomain>.<base-domain>/my-app/`.

## Architecture

- **`server/relay.py`** -- a single-file FastAPI server. Routes HTTP requests
  and relayed WebSocket frames between a browser and the PC agent's local
  app over one outbound WebSocket connection the PC agent keeps open. State
  (accounts, registered apps) persists to a small SQLite file
  (`server/data/relay.db` by default).
- **`pc-agent/dashboard.py`** -- a local control panel (`http://127.0.0.1:8787`)
  for managing which local ports are exposed. Supervises one
  `tunnel_agent.py` process per exposed app, restarting it if it crashes.
- **`pc-agent/tunnel_agent.py`** -- the actual process that holds the
  WebSocket connection to the relay and proxies requests to the real local
  port. One of these runs per exposed app.

No database server, no Redis, no external dependencies beyond what's in
each `requirements.txt` -- this is meant to be small enough to read start
to finish in one sitting.

## Configuration

Set these as environment variables on the relay server (see
`server/docker-compose.yml`):

| Variable | Default | Purpose |
|---|---|---|
| `RELAY_ADMIN_SECRET` | `change-me-before-deploying` | Gates account/app admin routes. **Change this before exposing the server to the internet.** |
| `RELAY_BASE_DOMAIN` | `localhost` | The domain your accounts' gateway subdomains live under, e.g. `relay.example.com` -> set to `example.com`. |
| `RELAY_CONTROL_SUBDOMAIN` | `relay` | The reserved subdomain the PC agent connects to (`wss://relay.example.com/...`). |
| `RELAY_DB_FILE` | `./data/relay.db` | Where account/app state is persisted. |
| `RELAY_COOKIE_SECURE` | `1` | Set to `0` only for local `http://` testing -- leave on `1` behind real TLS. |

For a real deployment reachable from the internet, put a reverse proxy
(Caddy, nginx, a Cloudflare Tunnel, etc.) in front for TLS -- the relay
server itself is bound to `127.0.0.1` in the provided `docker-compose.yml`.

## Testing

`server/test_relay_e2e.py` is a real end-to-end test: it starts the actual
server, registers an app, connects a fake tunnel agent over the real
WebSocket, and pushes a request through the full path (gateway login ->
tunnel socket -> fake local app -> back). No mocks, no extra test
dependency beyond what `requirements.txt` already installs.

```
cd server
pip install -r requirements.txt
python test_relay_e2e.py
```

## Admin: multiple accounts on one relay

If you want to share your relay with a few people rather than using it
solo, the admin routes (gated by `RELAY_ADMIN_SECRET`) create/list/delete
accounts and rotate keys:

```
curl -X POST http://127.0.0.1:8080/api/v1/users/create \
  -H "Content-Type: application/json" \
  -d '{"admin_secret": "<RELAY_ADMIN_SECRET>"}'
```

There's no billing, plans, or usage caps in this edition -- everyone with
an account on your own relay is assumed trusted, since you're the one who
created it.

## Hosted alternative

This repo is the DIY path: you own the VPS, the domain, the TLS
certificate, and the uptime. That's the right call if you want full
control or you're already running infrastructure anyway -- but it's real
setup and real ongoing maintenance (renewing certs, patching the box,
noticing if it goes down).

**[novarelynx.com](https://novarelynx.com)** is the same product, hosted:
sign up, get an API key immediately, no server to provision or maintain.
It's the faster path if you just want your app on the internet right now,
or if "running a VPS" isn't something you want to own.

| | Self-host (this repo) | Hosted ([novarelynx.com](https://novarelynx.com)) |
|---|---|---|
| Setup time | `docker compose up` + your own DNS/TLS | Sign up, get an API key -- done |
| Who maintains the server | You | NovaTeka LLC |
| Cost | Your own VPS | Free tier available, paid plans for more bandwidth |
| Control | Full -- it's your code, your box | Managed for you |

Both use the exact same protocol -- `dashboard.py`/`tunnel_agent.py` from
this repo work completely unmodified against either one, you just point
the "Relay address" setting at a different host. Nothing about switching
between them later requires changing your local app.

## License

[PolyForm Noncommercial License 1.0.0](LICENSE) -- free for personal and
noncommercial use. For commercial use, contact support@novarelynx.com to
arrange a commercial license.
