#!/usr/bin/env python3
"""server-monitor: проверки ping/TCP/HTTP, дашборд, Telegram-алерты."""
from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("MONITOR_CONFIG", BASE_DIR / "config" / "servers.json"))
STATE_PATH = Path(os.environ.get("MONITOR_STATE", BASE_DIR / "state.json"))
WEB_DIR = BASE_DIR / "web"

PING_TIMEOUT = 2
TCP_TIMEOUT = 3
HTTP_TIMEOUT = 5


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def atomic_write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def check_ping(host: str) -> dict:
    started = time.monotonic()
    try:
        result = subprocess.run(
            ["ping", "-c", "1", "-W", str(PING_TIMEOUT), host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=PING_TIMEOUT + 1,
        )
        ok = result.returncode == 0
        return {"ok": ok, "latency_ms": round((time.monotonic() - started) * 1000, 1)}
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return {"ok": False, "error": str(exc)}


def check_tcp(host: str, port: int) -> dict:
    started = time.monotonic()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(TCP_TIMEOUT)
    try:
        sock.connect((host, port))
        return {"ok": True, "port": port, "latency_ms": round((time.monotonic() - started) * 1000, 1)}
    except OSError as exc:
        return {"ok": False, "port": port, "error": str(exc)}
    finally:
        sock.close()


def check_http(url: str, expected_code: int = 200, keyword: str | None = None) -> dict:
    started = time.monotonic()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers={"User-Agent": "server-monitor/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as resp:
            body = resp.read(65536).decode("utf-8", errors="replace")
            code = resp.getcode()
            ok = code == expected_code and (keyword is None or keyword in body)
            return {
                "ok": ok,
                "url": url,
                "code": code,
                "expected_code": expected_code,
                "keyword": keyword,
                "keyword_found": keyword is None or keyword in body,
                "latency_ms": round((time.monotonic() - started) * 1000, 1),
            }
    except urllib.error.HTTPError as exc:
        return {"ok": False, "url": url, "code": exc.code, "error": str(exc)}
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
        return {"ok": False, "url": url, "error": str(exc)}


def check_server(server: dict) -> dict:
    host = server["host"]
    checks_cfg = server.get("checks", {})
    result: dict = {"name": server["name"], "host": host, "checks": {}}

    if checks_cfg.get("ping"):
        result["checks"]["ping"] = check_ping(host)

    tcp_ports = checks_cfg.get("tcp") or []
    if tcp_ports:
        result["checks"]["tcp"] = [check_tcp(host, p) for p in tcp_ports]

    http_targets = checks_cfg.get("http") or []
    if http_targets:
        result["checks"]["http"] = [
            check_http(t["url"], t.get("expected_code", 200), t.get("keyword"))
            for t in http_targets
        ]

    result["ok"] = _is_server_ok(result["checks"])
    return result


def _is_server_ok(checks: dict) -> bool:
    if "ping" in checks and not checks["ping"]["ok"]:
        return False
    for item in checks.get("tcp", []):
        if not item["ok"]:
            return False
    for item in checks.get("http", []):
        if not item["ok"]:
            return False
    return True


def send_telegram(cfg: dict, text: str) -> None:
    tg = cfg.get("telegram") or {}
    if not tg.get("enabled"):
        return
    token = tg.get("bot_token")
    chat_id = tg.get("chat_id")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        urllib.request.urlopen(url, data=data, timeout=HTTP_TIMEOUT).read()
    except Exception as exc:
        print(f"[telegram] send failed: {exc}", file=sys.stderr)


class Poller:
    def __init__(self, config: dict):
        self.config = config
        self.interval = int(config.get("interval", 30))
        self.lock = threading.Lock()
        self.snapshot: dict = {"generated_at": 0, "servers": []}
        self.prev_status: dict[str, bool] = {}
        if STATE_PATH.exists():
            try:
                self.snapshot = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                self.prev_status = {s["name"]: s["ok"] for s in self.snapshot.get("servers", [])}
            except Exception:
                pass

    def run_forever(self) -> None:
        while True:
            try:
                self._poll_once()
            except Exception as exc:
                print(f"[poller] error: {exc}", file=sys.stderr)
            time.sleep(self.interval)

    def _poll_once(self) -> None:
        servers = self.config.get("servers", [])
        with ThreadPoolExecutor(max_workers=max(1, len(servers))) as pool:
            results = list(pool.map(check_server, servers))

        snapshot = {
            "generated_at": int(time.time()),
            "interval": self.interval,
            "servers": results,
        }
        with self.lock:
            self.snapshot = snapshot
        atomic_write_json(STATE_PATH, snapshot)
        self._notify_changes(results)

    def _notify_changes(self, results: list[dict]) -> None:
        for srv in results:
            name = srv["name"]
            ok = srv["ok"]
            prev = self.prev_status.get(name)
            if prev is None:
                self.prev_status[name] = ok
                continue
            if prev != ok:
                if ok:
                    msg = f"✅ <b>{name}</b> восстановлен ({srv['host']})"
                else:
                    failed = self._summarize_failure(srv)
                    msg = f"❌ <b>{name}</b> упал ({srv['host']})\n{failed}"
                send_telegram(self.config, msg)
                self.prev_status[name] = ok

    @staticmethod
    def _summarize_failure(srv: dict) -> str:
        checks = srv["checks"]
        parts: list[str] = []
        if "ping" in checks and not checks["ping"]["ok"]:
            parts.append("ping fail")
        for item in checks.get("tcp", []):
            if not item["ok"]:
                parts.append(f"tcp {item['port']} fail")
        for item in checks.get("http", []):
            if not item["ok"]:
                code = item.get("code", "?")
                parts.append(f"http {item['url']} ({code})")
        return ", ".join(parts) or "unknown"


def _is_authorized(handler: BaseHTTPRequestHandler, auth_cfg: dict) -> bool:
    if not auth_cfg.get("enabled"):
        return True
    expected = auth_cfg.get("token") or ""
    if not expected:
        return True
    hdr = handler.headers.get("Authorization", "")
    if hdr.startswith("Bearer ") and hdr[7:] == expected:
        return True
    qs = urllib.parse.urlparse(handler.path).query
    params = urllib.parse.parse_qs(qs)
    token = (params.get("token") or [""])[0]
    return token == expected


def make_handler(poller: Poller, config: dict):
    auth_cfg = config.get("auth") or {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            sys.stderr.write("[http] %s - %s\n" % (self.address_string(), fmt % args))

        def _send_json(self, status: int, payload) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_file(self, path: Path, content_type: str) -> None:
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            if path in ("/", "/index.html"):
                self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
                return

            if path == "/api/auth-required":
                self._send_json(200, {"required": bool(auth_cfg.get("enabled") and auth_cfg.get("token"))})
                return

            if path == "/api/state":
                if not _is_authorized(self, auth_cfg):
                    self._send_json(401, {"error": "unauthorized"})
                    return
                with poller.lock:
                    self._send_json(200, poller.snapshot)
                return

            self.send_error(404)

    return Handler


def main() -> None:
    if not CONFIG_PATH.exists():
        print(f"config not found: {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    config = load_config()
    poller = Poller(config)

    t = threading.Thread(target=poller.run_forever, daemon=True)
    t.start()

    port = int(config.get("dashboard_port", 8888))
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(poller, config))
    print(f"server-monitor listening on http://0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
