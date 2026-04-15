# TrueConf Webhook Bot

> [Русская версия](README-ru.md)

Incoming webhook bot for TrueConf Server chats. Inside any chat you can generate
a personal URL — an external service sends a plain `POST` to it and the text
(with optional attachments by URL) lands in the chat. A Slack/Mattermost-style
incoming webhook for TrueConf.

## Features

- Commands `/webhook_create`, `/webhook_list`, `/webhook_revoke` right in chat.
- The full URL is delivered in a direct message to the author, so the secret
  never appears in the group chat history.
- `/webhook_list` always shows a masked token (`abcd…wxyz`).
- Multiple hooks per chat (one per integration).
- Optional "chat admins only" restriction (`WEBHOOK_ADMIN_ONLY`).
- Attachments by external URL: `photo` (compressed) or `document` (any file).
- Per-token rate limit.
- Automatic JWT refresh when authenticating by username/password.

## TrueConf Server preparation

1. Create a regular user account for the bot on your TrueConf Server (e.g.
   `webhook_bot`) and set a password.
2. Make sure the bot account has permission to write in the chats where it
   will be used — invite it as a participant beforehand.
3. Recommended authentication mode is username/password: the service will
   transparently refresh the JWT every ~25 days (the JWT itself lives 30 days).

### Alternative: issue a token manually

```bash
curl -X POST https://video.example.com/bridge/api/client/v1/oauth/token \
  -H "Content-Type: application/json" \
  -d '{"client_id":"chat_bot","grant_type":"password","username":"webhook_bot","password":"..."}'
```

Put the `access_token` into `TRUECONF_TOKEN`. The token lives 30 days and must
be refreshed manually in this mode.

## Install and run

```bash
python -m venv .venv
source .venv/bin/activate     # Linux / macOS
.venv\Scripts\activate        # Windows PowerShell

pip install --pre -e .
cp .env.example .env
# edit .env

python -m trueconf_webhook_bot
```

## Configuration (.env)

| Variable | Required | Default |
|---|---|---|
| `TRUECONF_SERVER` | yes | — |
| `TRUECONF_TOKEN` | one of the auth options | — |
| `TRUECONF_USERNAME` + `TRUECONF_PASSWORD` | one of the auth options | — |
| `TRUECONF_VERIFY_SSL` | no | `true` |
| `TRUECONF_WEB_PORT` | no | `443` |
| `WEBHOOK_PUBLIC_URL` | yes | — |
| `WEBHOOK_HTTP_HOST` | no | `0.0.0.0` |
| `WEBHOOK_HTTP_PORT` | no | `8080` |
| `WEBHOOK_STORAGE_PATH` | no | `data/webhooks.json` |
| `WEBHOOK_ADMIN_ONLY` | no | `true` |
| `WEBHOOK_RATE_LIMIT_PER_MINUTE` | no | `60` |
| `WEBHOOK_RATE_LIMIT_PER_IP_PER_MINUTE` | no | `120` |
| `WEBHOOK_MAX_UPLOAD_MB` | no | `25` |
| `WEBHOOK_MAX_ATTACHMENTS` | no | `10` |

Authentication priority: if `TRUECONF_TOKEN` is set, it is used; otherwise the
pair `TRUECONF_USERNAME` + `TRUECONF_PASSWORD` is required.

## Incoming POST format

`POST <WEBHOOK_PUBLIC_URL>/hook/<token>` accepts two content types; field
layout is symmetric between them.

### JSON (`Content-Type: application/json`)

```json
{
  "text": "Deploy succeeded",
  "parse_mode": "markdown",
  "images": [
    {"url": "https://example.com/build.png"}
  ],
  "files": [
    {"url": "https://example.com/log.txt", "filename": "build.log"}
  ]
}
```

- `text` is optional if at least one attachment is present.
- `parse_mode`: `text` (default), `markdown`, `html`.
- `images[]` → sent as compressed photos. `files[]` → sent as arbitrary documents.
- `filename` on `files[]` is optional (handy when the URL has no nice name).

### multipart/form-data

Form fields:

- `text`, `parse_mode` — plain string fields.

Repeatable parts (semantics in the part name):

| Part name | Value | Effect |
|---|---|---|
| `image_url` | URL string | external photo |
| `file_url`  | URL string | external document |
| `image`     | binary part | uploaded photo (filename from `Content-Disposition`) |
| `file`      | binary part | uploaded document (filename from `Content-Disposition`) |

Per-attachment captions are not supported — use the top-level `text` for
context, or send multiple POST requests when per-file descriptions are needed.

### Limits and security

- At most `WEBHOOK_MAX_ATTACHMENTS` items across `images + files` per request.
- Body size limited by `WEBHOOK_MAX_UPLOAD_MB`.
- Every URL is validated: only `http`/`https`, private/loopback/link-local
  addresses and known metadata hostnames are rejected (SSRF protection).
- Per-IP rate limit is applied before the token lookup (prevents token
  enumeration); per-token rate limit is applied after.

### Response codes

| Code | Meaning |
|---|---|
| `200` | Delivered |
| `400` | Invalid payload |
| `404` | Unknown or revoked token |
| `429` | Rate limit (with `Retry-After` header) |
| `502` | Partial delivery failure (details in body) |

## Deploy with Docker

The quickest path is `docker compose`. All settings live directly in
`docker-compose.yml` under the `environment:` block — no separate `.env` file
is required.

```bash
# edit docker-compose.yml:
#   TRUECONF_SERVER, TRUECONF_USERNAME, TRUECONF_PASSWORD, WEBHOOK_PUBLIC_URL
docker compose up -d --build
docker compose logs -f
```

Sensitive values (`TRUECONF_PASSWORD`, `TRUECONF_TOKEN`) can be kept out of the
compose file via `${VAR}` interpolation and exported from shell/CI, so they
never end up in git.

Hook state lives in the named volume `webhook-data` and survives container
rebuilds.

Manual run without Compose:

```bash
docker build -t trueconf-webhook-bot .
docker run -d --name trueconf-webhook-bot \
  --restart unless-stopped \
  -e TRUECONF_SERVER=video.example.com \
  -e TRUECONF_USERNAME=webhook_bot \
  -e TRUECONF_PASSWORD=... \
  -e WEBHOOK_PUBLIC_URL=https://bot.example.com \
  -p 8080:8080 \
  -v trueconf-webhook-data:/app/data \
  trueconf-webhook-bot
```

A built-in healthcheck pings `GET /readyz` every 30 seconds; if the bot loses
its connection to TrueConf the container status becomes `(unhealthy)`. Docker
does not restart containers on healthcheck failure on its own — the status is
a signal for operators or for an orchestrator (Kubernetes, Nomad). For
unattended recovery combine `restart: unless-stopped` with the healthcheck.

The default compose file binds port 8080 to loopback only
(`"127.0.0.1:8080:8080"`) and expects a TLS-terminating reverse proxy
(Caddy, Traefik, nginx) in front. Replace with `"8080:8080"` to expose
directly, but note that `WEBHOOK_PUBLIC_URL` must always point at the
externally reachable address (typically behind TLS).

## Deploy with systemd

```ini
# /etc/systemd/system/trueconf-webhook-bot.service
[Unit]
Description=TrueConf Webhook Bot
After=network-online.target

[Service]
Type=simple
User=trueconf-webhook
WorkingDirectory=/opt/trueconf-webhook-bot
EnvironmentFile=/opt/trueconf-webhook-bot/.env
ExecStart=/opt/trueconf-webhook-bot/.venv/bin/python -m trueconf_webhook_bot
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

When using a static `TRUECONF_TOKEN`, remember to refresh it monthly (or switch
to username/password mode).

## Security

- A webhook URL is a secret — treat it like a password.
- Leaking a single URL compromises only that one hook. Revoke it to regain
  control.
- `/webhook_list` never reveals the full token.
- The token is shown exactly once, in a direct message to the creator.
- Publish the HTTP endpoint through a reverse proxy with TLS; the host pointed
  at by `WEBHOOK_PUBLIC_URL` can differ from the internal
  `WEBHOOK_HTTP_HOST/PORT`.
