# server-monitor

Лёгкая система мониторинга серверов для Ubuntu 24.04 на чистом Python 3 (stdlib, без pip-зависимостей).

Состоит из двух компонентов:

- **server-monitor** — запускается на каждом сервере, выполняет проверки (ping, TCP-порты, HTTP/HTTPS с проверкой кода ответа и ключевого слова), отдаёт JSON и веб-дашборд на порту `8888`.
- **monitor-aggregator** — запускается на одной управляющей машине, опрашивает несколько `server-monitor` и сводит всё в единый дашборд на порту `9000` со вкладками по ДЦ.

Оба компонента поддерживают Bearer-токен авторизацию и Telegram-алерты при смене статуса.

## Установка

### server-monitor (на каждом сервере)

```bash
sudo bash install-monitor.sh \
  --repo https://github.com/Sharkisfinetoo/ServerStatus.git \
  --branch main
```

После установки:

1. Отредактируй `/opt/server-monitor/server-monitor/config/servers.json` — впиши список серверов, токен, Telegram.
2. `systemctl restart server-monitor`
3. Открой `http://<host>:8888/` и войди по токену.

### monitor-aggregator (на управляющей машине)

```bash
sudo bash install-aggregator.sh \
  --repo https://github.com/Sharkisfinetoo/ServerStatus.git \
  --branch main
```

После установки:

1. Отредактируй `/opt/monitor-aggregator/monitor-aggregator/config/aggregator.json` — список инстансов, токен агрегатора и токен для опроса `server-monitor`.
2. `systemctl restart monitor-aggregator`
3. Открой `http://<host>:9000/`.

## Локальный запуск

```bash
python3 server-monitor/monitor.py
python3 monitor-aggregator/aggregator.py
```

Конфиги читаются из `server-monitor/config/servers.json` и `monitor-aggregator/config/aggregator.json` (либо из путей в переменных `MONITOR_CONFIG` / `AGGREGATOR_CONFIG`).

## Конфигурация

### server-monitor

```json
{
  "interval": 30,
  "dashboard_port": 8888,
  "auth": { "enabled": true, "token": "секрет" },
  "telegram": { "enabled": true, "bot_token": "123:AAA...", "chat_id": "987654321" },
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

### monitor-aggregator

```json
{
  "interval": 20,
  "dashboard_port": 9000,
  "auth": { "enabled": true, "token": "токен-дашборда" },
  "telegram": { "enabled": true, "bot_token": "...", "chat_id": "..." },
  "instances_auth_token": "токен-от-server-monitor",
  "instances": [
    { "name": "DC-1 / Москва", "url": "http://192.168.1.10:8888/api/state" },
    { "name": "DC-2 / Берлин", "url": "http://10.0.0.5:8888/api/state" }
  ]
}
```

## API

- `GET /api/auth-required` — публичный, отвечает `{ "required": bool }`.
- `GET /api/state` — требует Bearer-токен, отдаёт текущий снапшот.

Авторизация:

```bash
curl -H "Authorization: Bearer <TOKEN>" http://host:8888/api/state
# или
curl "http://host:8888/api/state?token=<TOKEN>"
```

## Особенности

- Все проверки выполняются параллельно (ThreadPoolExecutor).
- Telegram-алерт срабатывает только при **смене** статуса — не спамит.
- Состояние сохраняется атомарно в `state.json` через `.tmp` файл.
- Веб-дашборд — чистый HTML/JS без фреймворков, автообновление каждые 15 сек.
- SSL-сертификаты не проверяются (для self-signed health-чеков).

## Управление сервисами

```bash
systemctl status  server-monitor
systemctl restart server-monitor
journalctl -u     server-monitor -f

systemctl status  monitor-aggregator
systemctl restart monitor-aggregator
journalctl -u     monitor-aggregator -f
```
