"""Клиент для стороннего AdminAntizapret (github.com/Kirito0098/AdminAntizapret).

Панель на втором сервере уже умеет выдавать одноразовые ссылки на скачивание
OpenVPN-профиля (см. /generate_one_time_download в её routes/config_routes.py):
TTL, лимит скачиваний и журнал аудита там свои. Мы не храним и не раздаём
файлы профилей сами — только логинимся под выделенным admin-аккаунтом панели
и просим её выпустить свежую одноразовую ссылку на пару файлов клиента.

У панели нет API-токена — только сессия по логину/паролю с CSRF-токеном на
форме входа, поэтому вход эмулируется как обычный браузер (cookiejar).
"""
from __future__ import annotations

import http.cookiejar
import json
import re
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request

CSRF_INPUT_RE = re.compile(r'<input\b[^>]*\bname="csrf_token"[^>]*>', re.IGNORECASE)
CSRF_VALUE_RE = re.compile(r'\bvalue="([^"]*)"')
PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _extract_csrf_token(html: str) -> str | None:
    """Достаёт value из <input name="csrf_token" ...> независимо от порядка атрибутов."""
    tag_match = CSRF_INPUT_RE.search(html)
    if not tag_match:
        return None
    value_match = CSRF_VALUE_RE.search(tag_match.group(0))
    return value_match.group(1) if value_match else None


class ProfileNotFound(Exception):
    pass


class AntizapretError(Exception):
    pass


class _SessionExpired(Exception):
    pass


class AntizapretClient:
    def __init__(self, server_cfg: dict, timeout: float = 10.0):
        self.id = server_cfg["id"]
        self.title = server_cfg.get("title") or self.id
        self.base_url = server_cfg["base_url"].rstrip("/")
        self.username = server_cfg["admin_username"]
        self.password = server_cfg["admin_password"]
        self.verify_tls = server_cfg.get("verify_tls", True)
        self.timeout = timeout
        self._lock = threading.Lock()
        self._jar = http.cookiejar.CookieJar()
        self._opener = self._build_opener()
        self._logged_in = False

    def _build_opener(self):
        ctx = ssl.create_default_context()
        if not self.verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar),
            urllib.request.HTTPSHandler(context=ctx),
        )

    def _open(self, method: str, path: str, data: dict | None = None):
        url = self.base_url + path
        body = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
        req = urllib.request.Request(
            url, data=body, method=method,
            headers={"User-Agent": "server-monitor-profiles/1.0"},
        )
        try:
            return self._opener.open(req, timeout=self.timeout)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise AntizapretError(f"не удалось подключиться к панели {self.base_url}: {exc}") from exc

    def _login(self) -> None:
        resp = self._open("GET", "/login")
        html = resp.read().decode("utf-8", "replace")
        csrf_token = _extract_csrf_token(html)
        if not csrf_token:
            raise AntizapretError("не удалось получить csrf_token со страницы логина панели")
        resp = self._open("POST", "/login", data={
            "csrf_token": csrf_token,
            "username": self.username,
            "password": self.password,
            "remember_me": "1",
        })
        resp.read()
        if resp.geturl().rstrip("/").endswith("/login"):
            self._logged_in = False
            raise AntizapretError("не удалось авторизоваться в AdminAntizapret (неверные логин/пароль?)")
        self._logged_in = True

    def _generate_one_time_link(self, filename: str) -> str | None:
        path = f"/generate_one_time_download/openvpn/{urllib.parse.quote(filename)}"
        try:
            resp = self._open("GET", path)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise
        ctype = resp.headers.get("Content-Type", "")
        raw = resp.read()
        if "application/json" not in ctype:
            raise _SessionExpired()
        payload = json.loads(raw.decode("utf-8"))
        if not payload.get("success"):
            raise AntizapretError(payload.get("message") or "панель отказала в выдаче ссылки")
        return payload["download_url"]

    def _generate_with_retry(self, filename: str) -> str | None:
        for attempt in (1, 2):
            try:
                return self._generate_one_time_link(filename)
            except _SessionExpired:
                if attempt == 2:
                    raise AntizapretError("сессия администратора панели не подтверждается")
                self._login()
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403) and attempt == 1:
                    self._login()
                    continue
                raise AntizapretError(f"панель ответила HTTP {exc.code}") from exc
        return None

    def get_download_links(self, profile_name: str) -> dict:
        if not PROFILE_NAME_RE.match(profile_name):
            raise ValueError("некорректное имя профиля")

        with self._lock:
            if not self._logged_in:
                self._login()

            antizapret_url = self._generate_with_retry(f"antizapret-{profile_name}.ovpn")
            vpn_url = self._generate_with_retry(f"vpn-{profile_name}.ovpn")

        if not antizapret_url and not vpn_url:
            raise ProfileNotFound(profile_name)

        return {"antizapret_url": antizapret_url, "vpn_url": vpn_url}
