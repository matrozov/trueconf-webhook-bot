# TrueConf Webhook Bot

A TrueConf Server bot built on top of [python-trueconf-bot](https://github.com/TrueConf/python-trueconf-bot)
that lets chat users generate incoming webhooks (Slack/Mattermost style).
External services POST to the returned URL and the message lands in the chat.

**Language policy**: all comments, docstrings, log messages and
CLAUDE.md itself are in English. The only Russian file in the project is
`README-ru.md`.

## Functional requirements

### Chat commands

- `/webhook_create <name>` — create a new webhook with the given name.
  - Name is required and unique within a chat.
  - The full URL is delivered **in a direct message** to the initiator via
    `bot.create_personal_chat(user_id)`.
  - The origin chat gets an impersonal acknowledgement without the URL.
  - If the DM delivery fails, the reserved record is revoked and the caller
    gets an error message.
- `/webhook_list` — list webhooks for the current chat with a masked token
  `abcd…wxyz`, the name, creation date, `last_used_at` and `usage_count`.
- `/webhook_revoke <name>` — revoke by name.

`show`/`reveal` commands are intentionally absent: the token is shown **once**
on creation. If you lose it, create a new one and revoke the old.

### Permissions

A global boolean flag `WEBHOOK_ADMIN_ONLY`:
- `true` — only `OWNER` or `ADMIN` roles can create/revoke webhooks.
- `false` — any chat participant can.

Roles come from `trueconf.enums.ChatParticipantRole`; "admin" roles are
`OWNER` and `ADMIN`. Verification uses
`bot.get_chat_participants(chat_id, page_size, page_number)` with pagination.

### Incoming webhook

The HTTP server (aiohttp) runs in the same event loop as `bot.run()`.
`POST <WEBHOOK_PUBLIC_URL>/hook/<token>` accepts two content types; the schemas
are symmetric by field.

#### JSON (`Content-Type: application/json`)

```json
{
  "text": "message",
  "parse_mode": "markdown",
  "images": [{"url": "https://example.com/pic.png"}],
  "files":  [{"url": "https://example.com/log.txt", "filename": "build.log"}]
}
```

- `text` is optional when at least one attachment is present.
- `parse_mode`: `text` (default), `markdown`, `html`.
- `images[]` -> `send_photo` (compressed photos). `files[]` -> `send_document` (any file).
- `filename` on `files[]` is optional (useful when the URL has no nice name).

#### multipart/form-data

Form fields:
- `text` — string (optional).
- `parse_mode` — string.

Repeatable parts (semantics in the part name):

| Part name | Value | Effect |
|---|---|---|
| `image_url` | URL string | `send_photo` + `URLInputFile` |
| `file_url`  | URL string | `send_document` + `URLInputFile` |
| `image`     | binary part | `send_photo` + `BufferedInputFile`, name from `Content-Disposition` |
| `file`      | binary part | `send_document` + `BufferedInputFile`, name from `Content-Disposition` |

Per-attachment captions are **not supported** in either JSON or multipart:
the top-level `text` covers context; when different captions per file are
truly needed, post multiple requests.

#### Common rules

- At most `len(images) + len(files) <= 10` per request.
- Body size limit: `WEBHOOK_MAX_UPLOAD_MB` (default 25 MB).
- **SSRF protection** for every URL: only `http`/`https`, no private /
  loopback / link-local / reserved addresses, no `localhost`, no cloud
  metadata hostnames.
- A per-IP rate limit is applied BEFORE the token lookup (blocks enumeration);
  a per-token rate limit is applied after.
- Replies to existing messages are **not supported**.
- Response codes: `200` ok, `400` invalid payload/URL, `404` unknown or revoked
  token, `429` rate limited (with `Retry-After`), `502` partial/total delivery
  failure (details in the body).

## Storage

A flat JSON file `data/webhooks.json` with atomic writes via `tmp + os.replace`
and an `asyncio.Lock` around mutations.

### Record schema

```json
{
  "id": "uuid4",
  "chat_id": "…",
  "name": "Jenkins CI",
  "token": "<plaintext urlsafe, 32 bytes>",
  "created_at": "2026-…T…Z",
  "created_by": "trueconf_user_id",
  "last_used_at": null,
  "usage_count": 0
}
```

### Token security model

- Tokens are stored **in plaintext**. This is a self-hosted tool deployed next
  to the TrueConf Server; the URL itself is the secret, as in Slack/Mattermost.
- Length: `secrets.token_urlsafe(32)` — ~190 bits of entropy.
- A chat may have **multiple** webhooks.
- URL = secret: never log fully, always mask in `/webhook_list`.
- No global shared secret.

## Authentication with TrueConf Server

A "bot" is a regular user account on the server. The token is issued by
`POST /bridge/api/client/v1/oauth/token` against login/password and lives for
**one month**.

Two mutually exclusive modes:

1. **Login/password (recommended)** — `Bot.from_credentials(server, username, password, ...)`.
   `get_auth_token()` is called once at startup. A long-running service needs
   a **refresh** mechanism (see below).
2. **Ready JWT** — an admin POSTs to the OAuth endpoint manually and puts
   `access_token` into `TRUECONF_TOKEN`. Must be rotated every month by hand.

Priority: `TRUECONF_TOKEN` -> fixed token; otherwise the pair
`TRUECONF_USERNAME` + `TRUECONF_PASSWORD` is required.

## Token refresh (facts verified in `python-trueconf-bot` sources)

Verified in `trueconf/client/bot.py` and `trueconf/utils/token.py`:

- `validate_token` in `Bot.__init__` checks `exp` — an expired JWT fails immediately.
- `self.__token` is private, no setter — only `_Bot__token` via name mangling.
- `__authorize()` runs on every (re)connect. `ApiErrorException` propagates
  and kills the loop.
- `__connect_and_listen` retries only on `ConnectionClosed` (fixed 0.5s sleep);
  for other errors `run()` crashes.
- A healthy WebSocket session is **not** broken by token expiry — the problem
  surfaces only on reconnect.
- `Dispatcher` is not tied to a `Bot` instance (just a list of routers) — safe to reuse.

### Refresh strategy (option A: hot-swap token + force reconnect)

Active only in `from_credentials` mode.

1. A background task waits until `exp - 5 days` (the `exp` is parsed from the JWT).
2. It calls `get_auth_token(...)` to obtain a fresh JWT.
3. Injects it: `bot._Bot__token = new_token`.
4. Forces reconnect: `await bot._ws.close()`. The `while not self._stop` inside
   `__connect_and_listen` catches `ConnectionClosed`, retries and invokes
   `__authorize()` with the new token.
5. Watchdog: `run()` is wrapped in a loop; on `ApiErrorException` or any other
   crash the supervisor forces a refresh and restarts `bot.start()` with
   exponential backoff.

On startup the supervisor checks the `_Bot__token` attribute for existence —
fail-fast if the library renamed it.

Fallback for ready-JWT mode: systemd with `Restart=always`, the admin rotates
the token manually.

## Architecture and layout

```
trueconf_webhook_bot/
  __init__.py
  __main__.py        — entrypoint, wiring, asyncio.gather
  config.py          — env loading, validation
  models.py          — Webhook dataclass
  storage.py         — JSON CRUD, asyncio.Lock, atomic writes
  bot_holder.py      — reference container for Bot, in-flight accounting
  permissions.py     — OWNER/ADMIN role check
  rate_limit.py      — per-key sliding window
  utils.py           — token masking, JWT exp parsing
  url_guard.py       — external URL validation (SSRF protection)
  handlers.py        — Router with /webhook_create|list|revoke
  http_server.py     — aiohttp POST /hook/{token}
  supervisor.py      — token refresh + run watchdog
data/webhooks.json
pyproject.toml
.env.example
README.md
README-ru.md
```

### Environment variables

- `TRUECONF_SERVER` — server address (`video.example.com`).
- `TRUECONF_TOKEN` — ready JWT (optional).
- `TRUECONF_USERNAME`, `TRUECONF_PASSWORD` — for `from_credentials`.
- `TRUECONF_VERIFY_SSL` — `true`/`false` (default `true`).
- `TRUECONF_WEB_PORT` — WebSocket port (default `443`).
- `WEBHOOK_PUBLIC_URL` — public base URL (`https://bot.example.com`).
- `WEBHOOK_HTTP_HOST`, `WEBHOOK_HTTP_PORT` — listen address (default `0.0.0.0:8080`).
- `WEBHOOK_STORAGE_PATH` — path to the JSON file (default `data/webhooks.json`).
- `WEBHOOK_ADMIN_ONLY` — `true`/`false` (default `true`).
- `WEBHOOK_RATE_LIMIT_PER_MINUTE` — per-token RPS limit (default `60`).
- `WEBHOOK_RATE_LIMIT_PER_IP_PER_MINUTE` — per-IP RPS limit (default `120`).
- `WEBHOOK_MAX_UPLOAD_MB` — max incoming POST body (default `25`).
- `WEBHOOK_MAX_ATTACHMENTS` — max attachments per request (default `10`).

## Deliberate non-goals

- No replies to existing messages via incoming webhooks.
- No hashed tokens (and consequently no `show` command).
- No global shared secret.
- `admin-only` is a global flag, not per chat.
- No SQL/external DB.
