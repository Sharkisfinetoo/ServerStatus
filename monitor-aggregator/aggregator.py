#!/usr/bin/env python3
"""monitor-aggregator: опрашивает несколько server-monitor и сводит в один дашборд."""
from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = Path(os.environ.get("AGGREGATOR_CONFIG", BASE_DIR / "config" / "aggregator.json"))
STATE_PATH = Path(os.environ.get("AGGREGATOR_STATE", BASE_DIR / "state.json"))
WEB_DIR = BASE_DIR / "web"

FETCH_TIMEOUT = 8
SESSION_TTL = 7 * 24 * 3600
SESSION_COOKIE = "ma_session"

_sessions: dict[str, float] = {}
_sessions_lock = threading.Lock()


def _new_session() -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    with _sessions_lock:
        for k in [k for k, exp in _sessions.items() if exp < now]:
            del _sessions[k]
        _sessions[token] = now + SESSION_TTL
    return token


def _is_valid_session(token: str) -> bool:
    if not token:
        return False
    with _sessions_lock:
        exp = _sessions.get(token)
        if exp is None:
            return False
        if exp < time.time():
            del _sessions[token]
            return False
        return True


def _drop_session(token: str) -> None:
    if not token:
        return
    with _sessions_lock:
        _sessions.pop(token, None)


def _get_cookie(handler, name: str) -> str:
    raw = handler.headers.get("Cookie", "")
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            if k == name:
                return v
    return ""


def load_config() -> dict:
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def atomic_write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def fetch_instance(instance: dict, instances_token: str) -> dict:
    name = instance["name"]
    url = instance["url"]
    started = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": "monitor-aggregator/1.0"})
    if instances_token:
        req.add_header("Authorization", f"Bearer {instances_token}")
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT, context=ctx) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return {
            "name": name,
            "url": url,
            "reachable": True,
            "fetch_ms": round((time.monotonic() - started) * 1000, 1),
            "state": payload,
        }
    except Exception as exc:
        return {
            "name": name,
            "url": url,
            "reachable": False,
            "error": str(exc),
            "fetch_ms": round((time.monotonic() - started) * 1000, 1),
            "state": None,
        }


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
        urllib.request.urlopen(url, data=data, timeout=FETCH_TIMEOUT).read()
    except Exception as exc:
        print(f"[telegram] send failed: {exc}", file=sys.stderr)


class Aggregator:
    def __init__(self, config: dict):
        self.config = config
        self.interval = int(config.get("interval", 20))
        self.lock = threading.Lock()
        self.snapshot: dict = {"generated_at": 0, "instances": []}
        self.prev_reachable: dict[str, bool] = {}
        if STATE_PATH.exists():
            try:
                self.snapshot = json.loads(STATE_PATH.read_text(encoding="utf-8"))
                self.prev_reachable = {i["name"]: i["reachable"] for i in self.snapshot.get("instances", [])}
            except Exception:
                pass

    def run_forever(self) -> None:
        while True:
            try:
                self._poll_once()
            except Exception as exc:
                print(f"[aggregator] error: {exc}", file=sys.stderr)
            time.sleep(self.interval)

    def _poll_once(self) -> None:
        instances = self.config.get("instances", [])
        token = self.config.get("instances_auth_token", "")
        with ThreadPoolExecutor(max_workers=max(1, len(instances))) as pool:
            results = list(pool.map(lambda i: fetch_instance(i, token), instances))

        snapshot = {
            "generated_at": int(time.time()),
            "interval": self.interval,
            "instances": results,
        }
        with self.lock:
            self.snapshot = snapshot
        atomic_write_json(STATE_PATH, snapshot)
        self._notify_dc_changes(results)

    def _notify_dc_changes(self, results: list[dict]) -> None:
        for inst in results:
            name = inst["name"]
            reachable = inst["reachable"]
            prev = self.prev_reachable.get(name)
            if prev is None:
                self.prev_reachable[name] = reachable
                continue
            if prev != reachable:
                if reachable:
                    msg = f"✅ ДЦ <b>{name}</b> снова доступен"
                else:
                    msg = f"🚨 ДЦ <b>{name}</b> недоступен: {inst.get('error', 'unknown')}"
                send_telegram(self.config, msg)
                self.prev_reachable[name] = reachable


def _auth_mode(auth_cfg: dict) -> str:
    if not auth_cfg.get("enabled"):
        return "none"
    if auth_cfg.get("username") and auth_cfg.get("password"):
        return "userpass"
    if auth_cfg.get("token"):
        return "token"
    return "none"


def _is_authorized(handler: BaseHTTPRequestHandler, auth_cfg: dict) -> bool:
    mode = _auth_mode(auth_cfg)
    if mode == "none":
        return True
    if _is_valid_session(_get_cookie(handler, SESSION_COOKIE)):
        return True
    expected = auth_cfg.get("token") or ""
    if expected:
        hdr = handler.headers.get("Authorization", "")
        if hdr.startswith("Bearer ") and hmac.compare_digest(hdr[7:], expected):
            return True
        qs = urllib.parse.urlparse(handler.path).query
        params = urllib.parse.parse_qs(qs)
        tok = (params.get("token") or [""])[0]
        if tok and hmac.compare_digest(tok, expected):
            return True
    return False


def make_handler(agg: Aggregator, config: dict):
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
            if self.command != "HEAD":
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
            if self.command != "HEAD":
                self.wfile.write(data)

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            if path in ("/", "/index.html"):
                self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
                return

            if path == "/api/auth-required":
                mode = _auth_mode(auth_cfg)
                self._send_json(200, {"required": mode != "none", "mode": mode})
                return

            if path == "/api/state":
                if not _is_authorized(self, auth_cfg):
                    self._send_json(401, {"error": "unauthorized"})
                    return
                with agg.lock:
                    self._send_json(200, agg.snapshot)
                return

            if path == "/api/metrics":
                if not _is_authorized(self, auth_cfg):
                    self._send_json(401, {"error": "unauthorized"})
                    return
                qs = urllib.parse.parse_qs(parsed.query)
                inst_name = (qs.get("instance") or [""])[0]
                range_str = (qs.get("range") or ["1h"])[0]
                inst = next((i for i in agg.config.get("instances", []) if i.get("name") == inst_name), None)
                if not inst:
                    self._send_json(400, {"error": "instance not found"})
                    return
                base = inst["url"].rsplit("/", 1)[0]
                fwd_url = base + "/metrics?range=" + urllib.parse.quote(range_str)
                req = urllib.request.Request(fwd_url, headers={"User-Agent": "monitor-aggregator/1.0"})
                token = agg.config.get("instances_auth_token", "")
                if token:
                    req.add_header("Authorization", f"Bearer {token}")
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                try:
                    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT, context=ctx) as resp:
                        self._send_json(200, json.loads(resp.read().decode("utf-8")))
                except urllib.error.HTTPError as exc:
                    try:
                        err_body = json.loads(exc.read().decode("utf-8"))
                    except Exception:
                        err_body = {"error": str(exc)}
                    self._send_json(exc.code, err_body)
                except Exception as exc:
                    self._send_json(502, {"error": f"instance unreachable: {exc}"})
                return

            self.send_error(404)

        do_HEAD = do_GET

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            path = parsed.path

            if path == "/api/login":
                if _auth_mode(auth_cfg) != "userpass":
                    self._send_json(404, {"error": "login disabled"})
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                body = self.rfile.read(length) if length else b""
                try:
                    data = json.loads(body.decode("utf-8"))
                except Exception:
                    self._send_json(400, {"error": "bad json"})
                    return
                u = str(data.get("username", ""))
                p = str(data.get("password", ""))
                cfg_u = str(auth_cfg.get("username", ""))
                cfg_p = str(auth_cfg.get("password", ""))
                ok = (cfg_u and cfg_p
                      and hmac.compare_digest(u, cfg_u)
                      and hmac.compare_digest(p, cfg_p))
                if not ok:
                    self._send_json(401, {"error": "invalid credentials"})
                    return
                token = _new_session()
                payload = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header(
                    "Set-Cookie",
                    f"{SESSION_COOKIE}={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_TTL}",
                )
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            if path == "/api/logout":
                _drop_session(_get_cookie(self, SESSION_COOKIE))
                payload = json.dumps({"ok": True}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header(
                    "Set-Cookie",
                    f"{SESSION_COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0",
                )
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            if path == "/api/check":
                if not _is_authorized(self, auth_cfg):
                    self._send_json(401, {"error": "unauthorized"})
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length else b""
                try:
                    data = json.loads(raw.decode("utf-8"))
                except Exception:
                    self._send_json(400, {"error": "bad json"})
                    return
                inst_name = str(data.get("instance", ""))
                inst = next((i for i in agg.config.get("instances", []) if i.get("name") == inst_name), None)
                if not inst:
                    self._send_json(400, {"error": "instance not found"})
                    return
                base = inst["url"].rsplit("/", 1)[0]
                check_url = base + "/check"
                fwd_payload = json.dumps({
                    "type": data.get("type"),
                    "target": data.get("target"),
                    "expected_code": data.get("expected_code"),
                    "keyword": data.get("keyword"),
                }).encode("utf-8")
                req = urllib.request.Request(check_url, data=fwd_payload, method="POST",
                                             headers={"Content-Type": "application/json",
                                                      "User-Agent": "monitor-aggregator/1.0"})
                token = agg.config.get("instances_auth_token", "")
                if token:
                    req.add_header("Authorization", f"Bearer {token}")
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                try:
                    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT, context=ctx) as resp:
                        body_bytes = resp.read()
                    self._send_json(200, json.loads(body_bytes.decode("utf-8")))
                except urllib.error.HTTPError as exc:
                    try:
                        err_body = json.loads(exc.read().decode("utf-8"))
                    except Exception:
                        err_body = {"error": str(exc)}
                    self._send_json(exc.code, err_body)
                except Exception as exc:
                    self._send_json(502, {"error": f"instance unreachable: {exc}"})
                return

            self.send_error(404)

    return Handler


def main() -> None:
    if not CONFIG_PATH.exists():
        print(f"config not found: {CONFIG_PATH}", file=sys.stderr)
        sys.exit(1)
    config = load_config()
    agg = Aggregator(config)

    t = threading.Thread(target=agg.run_forever, daemon=True)
    t.start()

    port = int(config.get("dashboard_port", 9000))
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(agg, config))
    print(f"monitor-aggregator listening on http://0.0.0.0:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
