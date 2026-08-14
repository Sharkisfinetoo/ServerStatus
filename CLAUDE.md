# server-monitor — контекст проекта

## Что это

Система мониторинга серверов для Ubuntu 24.04, написанная на чистом Python 3 (без pip-зависимостей).
Состоит из двух компонентов:

### server-monitor
- Запускается на каждом сервере
- Проверяет: ping (ICMP), TCP-порты, HTTP/HTTPS (код ответа + ключевое слово)
- Все проверки параллельные (threading)
- Отдаёт JSON через `/api/state` на порту **8888**
- Встроенный веб-дашборд
- Алерты в Telegram при **смене** статуса (не спамит)
- Токен-авторизация дашборда (Bearer token)
- systemd-служба: `server-monitor.service`
- Устанавливается: `sudo bash install-monitor.sh`

### monitor-aggregator
- Запускается на одной управляющей машине
- Опрашивает несколько экземпляров server-monitor
- Единый дашборд на порту **9000** со вкладками по ДЦ
- Алерты при недоступности целого ДЦ
- Та же токен-авторизация
- systemd-служба: `monitor-aggregator.service`
- Устанавливается: `sudo bash install-aggregator.sh`
- **`/profiles`** — публичный лендинг для VPN-пользователей (без Bearer-токена дашборда): инструкция по подключению OpenVPN + форма «скачать профиль по имени». Выдачей и авторизацией файлов занимается сторонняя панель [AdminAntizapret](https://github.com/Kirito0098/AdminAntizapret) на VPN-сервере — aggregator только логинится туда под выделенным admin-аккаунтом и просит выпустить одноразовую ссылку. Подробности — ниже.

## Структура репозитория

```
server-monitor/
├── CLAUDE.md                    ← этот файл
├── install-monitor.sh           ← curl | bash установщик
├── install-aggregator.sh        ← curl | bash установщик
├── README.md
├── server-monitor/
│   ├── monitor.py               ← основной скрипт
│   ├── config/servers.json      ← конфиг серверов
│   └── web/index.html           ← дашборд
└── monitor-aggregator/
    ├── aggregator.py            ← основной скрипт
    ├── antizapret_client.py     ← клиент к сторонней панели AdminAntizapret (профили VPN)
    ├── config/aggregator.json   ← конфиг экземпляров + vpn_profiles
    └── web/
        ├── index.html           ← единый дашборд с логином
        └── profiles/
            ├── index.html       ← список серверов с профилями
            └── landing.html     ← лендинг: инструкция + скачивание профиля
```

## Конфиг server-monitor (servers.json)

```json
{
  "interval": 30,
  "dashboard_port": 8888,
  "auth": {
    "enabled": true,
    "token": "секретный-токен"
  },
  "telegram": {
    "enabled": true,
    "bot_token": "123:AAA...",
    "chat_id": "987654321"
  },
  "servers": [
    {
      "name": "Nginx",
      "host": "192.168.1.10",
      "checks": {
        "ping": true,
        "tcp": [22, 80, 443],
        "http": [
          { "url": "https://192.168.1.10/health", "expected_code": 200, "keyword": "ok" }
        ]
      }
    }
  ]
}
```

## Конфиг aggregator (aggregator.json)

```json
{
  "interval": 20,
  "dashboard_port": 9000,
  "auth": { "enabled": true, "token": "токен-для-дашборда" },
  "telegram": { "enabled": true, "bot_token": "...", "chat_id": "..." },
  "instances_auth_token": "токен-от-server-monitor",
  "instances": [
    { "name": "DC-1 / Москва", "url": "http://192.168.1.10:8888/api/state" },
    { "name": "DC-2 / Берлин", "url": "http://10.0.0.5:8888/api/state" }
  ],
  "vpn_profiles": {
    "enabled": true,
    "rate_limit": { "max_requests": 5, "window_seconds": 300 },
    "servers": [
      {
        "id": "dc1",
        "title": "DC-1 / Москва",
        "base_url": "https://vpn1.example.com",
        "admin_username": "profiles-bot",
        "admin_password": "пароль-выделенного-admin-аккаунта",
        "verify_tls": true
      }
    ]
  }
}
```

### vpn_profiles — лендинг для VPN-пользователей

- Каждый элемент `servers` — это один экземпляр AdminAntizapret (порт по умолчанию — обычно 443/своя схема, указывается прямо в `base_url`) и один лендинг на `/profiles/<id>`. Список всех лендингов — `/profiles`.
- `admin_username`/`admin_password` — **отдельный** учётный аккаунт с ролью `admin` в самой панели AdminAntizapret, заведённый только для этой интеграции (не переиспользуйте личный логин админа — пароль лежит в открытом виде в `aggregator.json`, как и `telegram.bot_token`).
- Aggregator логинится на `{base_url}/login` (эмулирует браузер: забирает csrf_token со страницы, шлёт форму, хранит cookie сессии в памяти процесса) и дёргает `{base_url}/generate_one_time_download/openvpn/<filename>` для файлов `antizapret-<name>.ovpn` и `vpn-<name>.ovpn` — это штатный эндпоинт панели для одноразовых ссылок. Сама выдача файла, TTL, лимит скачиваний и журнал аудита — полностью на стороне AdminAntizapret; ServerStatus файлы профилей не хранит и не проксирует.
- `<name>` — «имя профиля», которое администратор сообщает пользователю лично (по сути секрет). Страница `/profiles/<id>` публичная, без Bearer-токена дашборда; единственная защита — это имя + rate-limit (`vpn_profiles.rate_limit`, по IP, дефолт 5 запросов / 5 минут на пару id+IP).
- Если понадобится сменить пароль `profiles-bot` или TTL/лимит одноразовых ссылок — TTL и лимит скачиваний настраиваются `.env`-переменными самого AdminAntizapret (`QR_DOWNLOAD_TOKEN_TTL_SECONDS`, `QR_DOWNLOAD_TOKEN_MAX_DOWNLOADS`), не в этом репозитории.

## Ключевые технические решения

- **Нет внешних зависимостей** — только stdlib Python: `urllib`, `socket`, `subprocess`, `threading`, `http.server`
- **Авторизация** — Bearer token в заголовке `Authorization` или query-параметр `?token=`
- **Telegram** — алерт только при смене `ok → fail` или `fail → ok`, состояние хранится в `prev_status: dict[str, bool]`
- **Параллельность** — каждый сервер опрашивается в отдельном потоке, результаты собираются через список буферов
- **Дашборд** — чистый HTML/JS без фреймворков, автообновление каждые 15 сек, sessionStorage для токена
- **State persistence** — `state.json` перезаписывается атомарно через `.tmp` файл
- **antizapret_client.py** — тоже без внешних зависимостей: `http.cookiejar` + `urllib.request` вместо `requests`, csrf_token вытаскивается регуляркой из HTML формы логина AdminAntizapret

## Команды для работы

```bash
# Запустить локально для разработки
python3 server-monitor/monitor.py
python3 monitor-aggregator/aggregator.py

# Проверить синтаксис
python3 -m py_compile server-monitor/monitor.py
python3 -m py_compile monitor-aggregator/aggregator.py
python3 -m py_compile monitor-aggregator/antizapret_client.py

# После изменений на сервере
systemctl restart server-monitor
systemctl restart monitor-aggregator
journalctl -u server-monitor -f

# Тест API
curl http://localhost:8888/api/state
curl -H "Authorization: Bearer токен" http://localhost:8888/api/state

# Тест Telegram
curl "https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<ID>&text=test"

# Тест лендинга с профилями (aggregator)
curl http://localhost:9000/profiles/api/list
curl -X POST http://localhost:9000/profiles/dc1/api/download \
  -H "Content-Type: application/json" -d '{"name":"ivan"}'

# Push на GitHub
git add . && git commit -m "описание" && git push
```
