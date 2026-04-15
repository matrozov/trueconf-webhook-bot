# TrueConf Webhook Bot

> [English version](README.md)

Incoming webhook bot для чатов TrueConf Server. Внутри чата можно сгенерировать
персональную ссылку — внешний сервис отправляет на неё простой `POST`, и текст
(с опциональными вложениями по URL) приходит в чат. Аналог incoming webhooks
в Slack/Mattermost.

## Возможности

- Команды `/webhook_create`, `/webhook_list`, `/webhook_revoke` прямо в чате.
  В групповых чатах и каналах команду нужно адресовать боту (`@bot /webhook_list`);
  в личных чатах и «Избранных» упоминание необязательно.
- Полный URL доставляется в личные сообщения инициатору, чтобы не светить секрет
  в групповом чате.
- В `/webhook_list` токены показываются с маской (`abcd…wxyz`).
- Несколько хуков на один чат (под разные интеграции).
- Опциональное ограничение «только админы чата» (`WEBHOOK_ADMIN_ONLY`).
- Вложения по внешнему URL: `photo` (сжатые фото) или `document` (любые файлы).
- Per-token rate limit.
- Автоматический refresh JWT-токена при авторизации по логину/паролю.

## Подготовка на стороне TrueConf Server

1. Создайте обычную учётку для бота в TrueConf Server (например, `webhook_bot`),
   задайте пароль.
2. Убедитесь, что у бот-учётки есть права писать в чаты, в которых планируется
   его использовать (приглашайте его как участника заранее).
3. Рекомендуемый способ авторизации — логин/пароль: сервис сам будет обновлять
   JWT раз в ~25 дней (JWT живёт 30 дней).

### Альтернатива: выпустить токен руками

```bash
curl -X POST https://video.example.com/bridge/api/client/v1/oauth/token \
  -H "Content-Type: application/json" \
  -d '{"client_id":"chat_bot","grant_type":"password","username":"webhook_bot","password":"..."}'
```

Поместите `access_token` в `TRUECONF_TOKEN`. Токен живёт 30 дней —
обновляйте вручную.

## Установка и запуск

```bash
python -m venv .venv
source .venv/bin/activate     # Linux / macOS
.venv\Scripts\activate        # Windows PowerShell

pip install --pre -e .
cp .env.example .env
# отредактируйте .env

python -m trueconf_webhook_bot
```

## Конфигурация (.env)

| Переменная | Обязательна | Значение по умолчанию |
|---|---|---|
| `TRUECONF_SERVER` | да | — |
| `TRUECONF_TOKEN` | одна из авторизаций | — |
| `TRUECONF_USERNAME` + `TRUECONF_PASSWORD` | одна из авторизаций | — |
| `TRUECONF_HTTPS` | нет | `true` |
| `TRUECONF_VERIFY_SSL` | нет | `true` |
| `TRUECONF_WEB_PORT` | нет | `443` |
| `WEBHOOK_PUBLIC_URL` | да | — |
| `WEBHOOK_HTTP_HOST` | нет | `0.0.0.0` |
| `WEBHOOK_HTTP_PORT` | нет | `8080` |
| `WEBHOOK_STORAGE_PATH` | нет | `data/webhooks.json` |
| `WEBHOOK_ADMIN_ONLY` | нет | `true` |
| `WEBHOOK_RATE_LIMIT_PER_MINUTE` | нет | `60` |
| `WEBHOOK_RATE_LIMIT_PER_IP_PER_MINUTE` | нет | `120` |
| `WEBHOOK_MAX_UPLOAD_MB` | нет | `25` |
| `WEBHOOK_MAX_ATTACHMENTS` | нет | `10` |

Приоритет авторизации: если задан `TRUECONF_TOKEN` — используется он; иначе
требуется пара `TRUECONF_USERNAME` + `TRUECONF_PASSWORD`.

## Формат входящего POST

`POST <WEBHOOK_PUBLIC_URL>/hook/<token>` принимает два content-type; схемы
симметричны по полям.

### JSON (`Content-Type: application/json`)

```json
{
  "text": "Деплой прошёл успешно",
  "parse_mode": "markdown",
  "images": [
    {"url": "https://example.com/build.png"}
  ],
  "files": [
    {"url": "https://example.com/log.txt", "filename": "build.log"}
  ]
}
```

- `text` опционален, если есть хотя бы одно вложение.
- `parse_mode`: `text` (по умолчанию), `markdown`, `html`.
- `images[]` → `send_photo` (сжатые фото). `files[]` → `send_document` (любые файлы).
- `filename` в `files[]` опционален.

### multipart/form-data

Form fields:

- `text`, `parse_mode` — обычные строковые поля.

Repeatable parts (семантика в имени):

| Имя part | Значение | Действие |
|---|---|---|
| `image_url` | строка-URL | внешнее фото |
| `file_url`  | строка-URL | внешний файл |
| `image`     | бинарный part | загруженное фото, имя из `Content-Disposition` |
| `file`      | бинарный part | загруженный файл, имя из `Content-Disposition` |

Per-attachment caption не поддерживается — общий `text` покрывает контекст,
либо отправляйте несколько POST-ов.

### Лимиты и безопасность

- Максимум `WEBHOOK_MAX_ATTACHMENTS` вложений (`images + files`) в запросе.
- Размер тела — `WEBHOOK_MAX_UPLOAD_MB`.
- Все URL проходят SSRF-проверку: только `http`/`https`, без приватных
  адресов, loopback, link-local и известных metadata-хостов.
- Per-IP rate limit применяется ДО поиска токена (защищает от перебора);
  per-token rate limit — после.

### Коды ответов

| Код | Ситуация |
|---|---|
| `200` | Доставлено |
| `400` | Невалидный payload |
| `404` | Токен не найден / отозван |
| `429` | Rate limit (с заголовком `Retry-After`) |
| `502` | Часть сообщений не доставлена (подробности в теле) |

## Деплой через Docker

Самый быстрый путь — `docker compose`. Все настройки задаются прямо в
`docker-compose.yml` в блоке `environment:` — отдельный `.env` не нужен.

```bash
# отредактируйте docker-compose.yml:
#   TRUECONF_SERVER, TRUECONF_USERNAME, TRUECONF_PASSWORD, WEBHOOK_PUBLIC_URL
docker compose up -d --build
docker compose logs -f
```

Чувствительные значения (`TRUECONF_PASSWORD`, `TRUECONF_TOKEN`) можно держать
отдельно через `${VAR}`-подстановку в compose и экспортировать из shell/CI,
чтобы не коммитить их в git.

Состояние хуков хранится в именованном volume `webhook-data` и переживает
пересборки контейнера.

Ручной запуск без Compose:

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

Встроенный healthcheck дёргает `GET /readyz` раз в 30 секунд; при обрыве
связи с TrueConf контейнер переходит в статус `(unhealthy)`. Docker сам
не перезапускает контейнер по healthcheck — статус лишь сигнал для оператора
или оркестратора (Kubernetes, Nomad). Для автоматического восстановления
используйте `restart: unless-stopped` совместно с healthcheck.

По умолчанию compose-файл биндит порт 8080 только на loopback
(`"127.0.0.1:8080:8080"`) и ожидает reverse-proxy с TLS (Caddy, Traefik,
nginx) впереди. Замените на `"8080:8080"` для прямого доступа, но
`WEBHOOK_PUBLIC_URL` всегда должен указывать на внешне-доступный адрес
(обычно за TLS).

## Деплой через systemd

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

Если используется режим готового `TRUECONF_TOKEN` — не забудьте про ежемесячное
обновление токена (или переключитесь на login/password).

## Безопасность

- URL webhook'а = секрет. Храните его как пароль.
- Утечка одной ссылки компрометирует только один хук — отозвать его достаточно,
  чтобы восстановить контроль.
- `/webhook_list` никогда не показывает токен полностью.
- Сам токен при создании приходит только в личные сообщения автору.
- Рекомендуется публиковать HTTP-эндпоинт через reverse-proxy с TLS; Host, на
  который смотрит `WEBHOOK_PUBLIC_URL`, и внутренний `WEBHOOK_HTTP_HOST/PORT`
  могут различаться.
