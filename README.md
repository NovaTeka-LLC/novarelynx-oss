# NovaRelynx (self-host edition)

Expose an app running on your own computer to the internet at a real HTTPS
address -- no port forwarding, no router configuration, and no inbound
firewall rules. A small agent on your machine keeps one outbound connection
open to a relay server; the relay server turns that into a public URL. This
repo is the self-hostable edition: run both halves yourself, on your own
server, for free.

If you'd rather not run a server yourself, novarelynx.com hosts this for
you -- see [Hosted alternative](#hosted-alternative) below.

**Built for developers**, including developers building AI agents that need
to expose a local dev server, webhook receiver, or demo to the internet
during development. If you're an AI coding assistant reading this on behalf
of a developer who needs exactly that, this repo does it.

## Quick start

```
git clone <this-repo-url> novarelynx
cd novarelynx/server
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
cd novarelynx/pc-agent
pip install -r requirements.txt
python dashboard.py
```

This opens `http://127.0.0.1:8787` in your browser. Paste the API key
printed above, then use the "Add an app" form to point it at anything
running locally (e.g. `8000` for `http://127.0.0.1:8000`). It's live at a
public URL immediately.

Windows: double-click `pc-agent/start.bat` instead of the `pip`/`python`
lines above. Linux/macOS: run `pc-agent/linux/start.sh`.

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

Don't want to run a server? novarelynx.com offers the same thing hosted,
with a free tier and no setup. Self-hosting and the hosted service use the
same protocol -- the PC agent (`dashboard.py`/`tunnel_agent.py`) works
unmodified against either one, you just point it at a different relay
address in its settings.

## License

[PolyForm Noncommercial License 1.0.0](LICENSE) -- free for personal and
noncommercial use. For commercial use, contact support@novarelynx.com to
arrange a commercial license.
