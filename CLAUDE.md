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
    ├── config/aggregator.json   ← конфиг экземпляров
    └── web/index.html           ← единый дашборд с логином
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
  ]
}
```

## Ключевые технические решения

- **Нет внешних зависимостей** — только stdlib Python: `urllib`, `socket`, `subprocess`, `threading`, `http.server`
- **Авторизация** — Bearer token в заголовке `Authorization` или query-параметр `?token=`
- **Telegram** — алерт только при смене `ok → fail` или `fail → ok`, состояние хранится в `prev_status: dict[str, bool]`
- **Параллельность** — каждый сервер опрашивается в отдельном потоке, результаты собираются через список буферов
- **Дашборд** — чистый HTML/JS без фреймворков, автообновление каждые 15 сек, sessionStorage для токена
- **State persistence** — `state.json` перезаписывается атомарно через `.tmp` файл

## Команды для работы

```bash
# Запустить локально для разработки
python3 server-monitor/monitor.py
python3 monitor-aggregator/aggregator.py

# Проверить синтаксис
python3 -m py_compile server-monitor/monitor.py
python3 -m py_compile monitor-aggregator/aggregator.py

# После изменений на сервере
systemctl restart server-monitor
systemctl restart monitor-aggregator
journalctl -u server-monitor -f

# Тест API
curl http://localhost:8888/api/state
curl -H "Authorization: Bearer токен" http://localhost:8888/api/state

# Тест Telegram
curl "https://api.telegram.org/bot<TOKEN>/sendMessage?chat_id=<ID>&text=test"

# Push на GitHub
git add . && git commit -m "описание" && git push
```
