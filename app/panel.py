from pathlib import Path
import json
import secrets
import os
import base64
import fcntl
import hmac
import ipaddress
import sqlite3
import socket
import tempfile
import threading
import time
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import wraps
from urllib.parse import quote, unquote, urlsplit
from zoneinfo import ZoneInfo
import requests

from flask import (
    Flask,
    render_template,
    request,
    redirect,
    url_for,
    abort,
    session,
    Response,
)
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix

# Directory containing MultiSub data files.
# Must be explicitly configured through MULTISUB_DATA_DIR.
if not os.environ.get("MULTISUB_DATA_DIR"):
    raise RuntimeError("MULTISUB_DATA_DIR is not configured")
BASE_DIR = Path(os.environ["MULTISUB_DATA_DIR"]).expanduser()
USERS_FILE = BASE_DIR / "users.json"
TRASH_FILE = BASE_DIR / "users-trash.json"
LOG_FILE = Path(
    os.environ.get("MULTISUB_LOG_FILE", str(BASE_DIR / "multisub.log"))
).expanduser()
STATE_LOCK_FILE = BASE_DIR / ".panel-state.lock"
LOGIN_DB_FILE = BASE_DIR / ".panel-login.sqlite3"
SOURCE_HEALTH_FILE = BASE_DIR / "source-health.json"

SUBSCRIPTION_BASE_URL = os.environ.get(
    "SUBSCRIPTION_BASE_URL", ""
).rstrip("/")
SUBSCRIPTION_PROTOCOLS = (
    "vless://", "vmess://", "trojan://", "ss://", "hysteria2://",
    "hy2://", "hysteria://", "tuic://", "wireguard://",
)
_subscription_cache = {}
_subscription_cache_lock = threading.Lock()
_log_file_lock = threading.Lock()
SUBSCRIPTION_TITLE = "MultiSub VPN"
SUBSCRIPTION_UPDATE_INTERVAL_HOURS = 1
PUBLIC_VPN_FEED_URL = (
    "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/"
    "refs/heads/main/BLACK_VLESS_RUS_mobile.txt"
)
PUBLIC_VPN_FEEDS = {
    "top150": {
        "label": "Публичный · TOP-150 mixed/mobile",
        "url": PUBLIC_VPN_FEED_URL,
    },
    "white-list-mobile": {
        "label": "Публичные белые списки · Reality Mobile",
        "url": (
            "https://raw.githubusercontent.com/igareck/vpn-configs-for-russia/"
            "refs/heads/main/Vless-Reality-White-Lists-Rus-Mobile.txt"
        ),
    },
}
SUBSCRIPTION_INFO_TEXT = (
    "🔒 Безопасные: под нашим управлением.\n"
    "🌐 Публичные: сторонние, используйте при необходимости.\n\n"
    "🔄 Автообновление каждый час.\n\n"
    "Проблемы с подключением? Обновите подписку вручную ↻"
)

PANEL_USERNAME = os.environ.get("PANEL_USERNAME", "admin")
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD", "")
PANEL_SECRET_KEY = os.environ.get("PANEL_SECRET_KEY", "")
PANEL_COOKIE_SECURE = os.environ.get("PANEL_COOKIE_SECURE", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}

if not PANEL_PASSWORD:
    raise RuntimeError("PANEL_PASSWORD is not configured")

if not PANEL_SECRET_KEY:
    raise RuntimeError("PANEL_SECRET_KEY is not configured")

# Keep templates configurable so the project can be installed anywhere.
TEMPLATE_DIR = Path(
    os.environ.get(
        "APP_TEMPLATES_DIR",
        str(Path(__file__).resolve().parent.parent / "templates"),
    )
).expanduser()

app = Flask(__name__, template_folder=str(TEMPLATE_DIR))
app.secret_key = PANEL_SECRET_KEY
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=PANEL_COOKIE_SECURE,
    MAX_CONTENT_LENGTH=2 * 1024 * 1024,
)


def get_csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def csrf_input():
    return Markup('<input type="hidden" name="csrf_token" value="{}">').format(
        get_csrf_token()
    )


@app.context_processor
def inject_template_helpers():
    return {"csrf_input": csrf_input, "csrf_token": get_csrf_token}


def serialized_state_change(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(STATE_LOCK_FILE, flags, 0o640)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return function(*args, **kwargs)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    return wrapped


def login_is_blocked(client_ip):
    now = time.time()
    with sqlite3.connect(LOGIN_DB_FILE, timeout=5) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS login_attempts ("
            "client_ip TEXT PRIMARY KEY, window_start REAL NOT NULL, "
            "attempts INTEGER NOT NULL, blocked_until REAL NOT NULL DEFAULT 0)"
        )
        db.execute(
            "DELETE FROM login_attempts WHERE window_start < ? AND blocked_until < ?",
            (now - 900, now),
        )
        row = db.execute(
            "SELECT window_start, attempts, blocked_until FROM login_attempts WHERE client_ip=?",
            (client_ip,),
        ).fetchone()
        if not row:
            return False
        window_start, attempts, blocked_until = row
        if now >= blocked_until and now - window_start >= 900:
            db.execute("DELETE FROM login_attempts WHERE client_ip=?", (client_ip,))
            return False
        return blocked_until > now


def record_login_failure(client_ip):
    now = time.time()
    with sqlite3.connect(LOGIN_DB_FILE, timeout=5) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS login_attempts ("
            "client_ip TEXT PRIMARY KEY, window_start REAL NOT NULL, "
            "attempts INTEGER NOT NULL, blocked_until REAL NOT NULL DEFAULT 0)"
        )
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT window_start, attempts FROM login_attempts WHERE client_ip=?",
            (client_ip,),
        ).fetchone()
        if not row or now - row[0] >= 900:
            window_start, attempts = now, 0
        else:
            window_start, attempts = row
        attempts += 1
        blocked_until = now + 900 if attempts >= 5 else 0
        db.execute(
            "INSERT INTO login_attempts(client_ip, window_start, attempts, blocked_until) "
            "VALUES(?,?,?,?) ON CONFLICT(client_ip) DO UPDATE SET "
            "window_start=excluded.window_start, attempts=excluded.attempts, "
            "blocked_until=excluded.blocked_until",
            (client_ip, window_start, attempts, blocked_until),
        )


def clear_login_failures(client_ip):
    with sqlite3.connect(LOGIN_DB_FILE, timeout=5) as db:
        db.execute("DELETE FROM login_attempts WHERE client_ip=?", (client_ip,))


def valid_redirect_target(target):
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return False
    parsed = urlsplit(target)
    return not parsed.scheme and not parsed.netloc


def valid_source_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(ch) < 32 for ch in value):
        return False
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() in {"http", "https"}
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and parsed.port in {None, 80, 443}
    )


def _public_source_addresses(hostname):
    try:
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError):
        return []
    if not addresses or not all(address.is_global for address in addresses):
        return []
    return [str(address) for address in sorted(addresses, key=lambda value: (value.version, int(value)))]


def _public_source_host(hostname):
    return bool(_public_source_addresses(hostname))


def _connections_from_payload(payload):
    text = payload.decode("utf-8-sig", errors="replace").strip()
    if not text:
        return []
    lines = text.splitlines()
    connections = [line.strip() for line in lines if line.strip().lower().startswith(SUBSCRIPTION_PROTOCOLS)]
    if connections:
        return connections
    compact = "".join(text.split())
    try:
        decoded = base64.b64decode(compact, validate=True)
    except (ValueError, base64.binascii.Error):
        return []
    decoded_text = decoded.decode("utf-8-sig", errors="replace")
    return [
        line.strip() for line in decoded_text.splitlines()
        if line.strip().lower().startswith(SUBSCRIPTION_PROTOCOLS)
    ]


def _label_connection(connection, source_kind):
    """Tag imported proxy names so owned and public nodes stay distinguishable in clients."""
    prefixes = {
        "personal": "[БЕЗОПАСНЫЙ]",
        "public": "[ПУБЛИЧНЫЙ]",
        "public_top150": "[ПУБЛИЧНЫЙ]",
        "public_white_list": "[ПУБЛИЧНЫЕ БЕЛЫЕ СПИСКИ]",
    }
    prefix = prefixes.get(source_kind, prefixes["personal"])
    known_prefixes = tuple(f"{value} " for value in prefixes.values()) + ("[НАШ] ",)

    def clean_name(value):
        name = str(value or "").strip()
        for known_prefix in known_prefixes:
            if name.startswith(known_prefix):
                return name[len(known_prefix):].strip()
        return name

    if connection.lower().startswith("vmess://"):
        payload = connection[8:]
        try:
            decoded = base64.b64decode(payload + "=" * (-len(payload) % 4))
            config = json.loads(decoded.decode("utf-8-sig"))
            if isinstance(config, dict):
                current_name = clean_name(config.get("ps", ""))
                config["ps"] = f"{prefix} {current_name}".strip()
                encoded = base64.b64encode(
                    json.dumps(config, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                ).decode("ascii")
                return "vmess://" + encoded
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return connection
        return connection

    try:
        parsed = urlsplit(connection)
        current_name = clean_name(unquote(parsed.fragment))
        labelled_name = f"{prefix} {current_name}".strip()
        return parsed._replace(fragment=quote(labelled_name, safe="")).geturl()
    except (ValueError, UnicodeError):
        return connection


def append_log_events(events):
    """Append a related group of sanitized events without interleaving workers."""
    now = datetime.now(timezone.utc)
    lines = []
    for level, message in events:
        timestamp = now.strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
        lines.append(f"{timestamp} [{level}] {message}\n")
    if not lines:
        return
    payload = "".join(lines).encode("utf-8")
    flags = os.O_CREAT | os.O_WRONLY | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(LOG_FILE, flags, 0o640)
        try:
            with _log_file_lock:
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    remaining = memoryview(payload)
                    while remaining:
                        written = os.write(fd, remaining)
                        if written <= 0:
                            raise OSError("Не удалось полностью записать журнал.")
                        remaining = remaining[written:]
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    except OSError:
        app.logger.exception("Не удалось записать событие в журнал MultiSub.")


def get_user_connections(token, sources, include_diagnostics=False, source_kinds=None):
    now = time.monotonic()
    with _subscription_cache_lock:
        cached = _subscription_cache.get(token)
        if cached and cached[0] > now:
            connections = list(cached[1])
            diagnostics = [dict(item) for item in cached[2]] if len(cached) > 2 else []
            if include_diagnostics:
                return connections, diagnostics, True
            return connections

    connections = []
    seen = set()
    diagnostics = []
    source_kinds = source_kinds or []
    for index, source in enumerate(sources[:50], start=1):
        started = time.perf_counter()
        http_status = None
        payload_size = 0
        outcome = "ERROR"
        source_connections = 0

        if not valid_source_url(source):
            diagnostics.append({
                "index": index,
                "host": source_host(source) if isinstance(source, str) else "Некорректный адрес",
                "outcome": "BLOCKED", "http_status": None,
                "latency_ms": None, "size": 0, "connections": 0,
            })
            continue
        hostname = urlsplit(source).hostname
        if not _public_source_host(hostname):
            diagnostics.append({
                "index": index, "host": hostname or "Неизвестный источник",
                "outcome": "BLOCKED", "http_status": None,
                "latency_ms": None, "size": 0, "connections": 0,
            })
            continue
        try:
            with requests.get(
                source,
                timeout=(3, 8),
                allow_redirects=False,
                stream=True,
                headers={"Accept": "text/plain"},
            ) as response:
                http_status = response.status_code
                if response.status_code == 200:
                    payload = bytearray()
                    for chunk in response.iter_content(65536):
                        payload.extend(chunk)
                        if len(payload) > 1024 * 1024:
                            payload_size = len(payload)
                            payload.clear()
                            break
                    else:
                        payload_size = len(payload)

                    if payload:
                        parsed_connections = _connections_from_payload(bytes(payload))
                        if parsed_connections:
                            outcome = "OK"
                            source_kind = (
                                source_kinds[index - 1]
                                if index - 1 < len(source_kinds) else "personal"
                            )
                            if source_kind == "public":
                                if source == PUBLIC_VPN_FEEDS["white-list-mobile"]["url"]:
                                    source_kind = "public_white_list"
                                elif source == PUBLIC_VPN_FEEDS["top150"]["url"]:
                                    source_kind = "public_top150"
                            for raw_connection in parsed_connections:
                                connection = _label_connection(raw_connection, source_kind)
                                if connection not in seen:
                                    seen.add(connection)
                                    connections.append(connection)
                                    source_connections += 1
                                    if len(connections) >= 500:
                                        break
                        else:
                            outcome = "ERROR"
                    else:
                        outcome = "ERROR"
                else:
                    outcome = "ERROR"
        except requests.Timeout:
            outcome = "TIMEOUT"
        except requests.RequestException:
            outcome = "ERROR"

        diagnostics.append({
            "index": index,
            "host": hostname,
            "outcome": outcome,
            "http_status": http_status,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "size": payload_size,
            "connections": source_connections,
        })

        if len(connections) >= 500:
            break

    with _subscription_cache_lock:
        _subscription_cache[token] = (
            time.monotonic() + 60,
            tuple(connections),
            tuple(dict(item) for item in diagnostics),
        )
    if include_diagnostics:
        return connections, diagnostics, False
    return connections


def log_subscription_request(token, diagnostics, connection_count, cached=False):
    user_key = token[:8]
    events = [("INFO", f"request user={user_key} sources={len(diagnostics)} cached={int(cached)}")]
    errors = 0
    for item in diagnostics:
        outcome = item["outcome"]
        http_outcome = "OK" if outcome == "OK" else "TIMEOUT" if outcome == "TIMEOUT" else "ERROR"
        status = item["http_status"] if item["http_status"] is not None else 0
        latency_ms = item["latency_ms"]
        duration = f"{latency_ms / 1000:.3f}s" if latency_ms is not None else "0s"
        events.append((
            "INFO",
            f"source {http_outcome} status={status} time={duration} size={item['size']}"
        ))
        events.append((
            "INFO",
            f"source={item['index']} {outcome} host={item['host']} "
            f"connections={item['connections']} time={duration}"
        ))
        if outcome != "OK":
            errors += 1
    events.append((
        "ERROR" if errors else "INFO",
        f"result user={user_key} connections={connection_count} errors={errors}",
    ))
    append_log_events(events)


@app.before_request
def require_login():
    host = (request.host or "").split(":", 1)[0].lower()
    if host in {"linebase.ru", "www.linebase.ru"}:
        if request.path != "/":
            abort(404)
        return

    if request.path == "/p" or request.path.startswith("/p/"):
        abort(404)

    public_endpoints = {
        "login",
        "health",
        "static",
        "subscription",
    }

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        expected = session.get("_csrf_token", "")
        supplied = request.form.get("csrf_token", "") or request.headers.get("X-CSRFToken", "")
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
            if request.endpoint == "login" and not session.get("authenticated"):
                session.clear()
                return redirect(url_for("login", expired="1"), code=303)
            abort(400, description="CSRF token is missing or invalid.")

    if request.endpoint in public_endpoints:
        return

    if not session.get("authenticated"):
        return redirect(url_for("login", next=request.path))


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if request.is_secure:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if session.get("authenticated") or request.endpoint == "login":
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated"):
        return redirect(url_for("index"))

    error = (
        "Форма входа устарела. Обнови данные и попробуй ещё раз."
        if request.args.get("expired") == "1"
        else None
    )

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        next_url = request.form.get("next", "")
        client_ip = request.remote_addr or "unknown"

        if login_is_blocked(client_ip):
            return render_template("login.html", error="Слишком много попыток. Подожди 15 минут.", next=""), 429

        if (
            hmac.compare_digest(username.encode("utf-8"), PANEL_USERNAME.encode("utf-8"))
            and hmac.compare_digest(password.encode("utf-8"), PANEL_PASSWORD.encode("utf-8"))
        ):
            session.clear()
            session["authenticated"] = True
            session["username"] = PANEL_USERNAME
            session["_csrf_token"] = secrets.token_urlsafe(32)
            clear_login_failures(client_ip)
            if valid_redirect_target(next_url):
                return redirect(next_url)
            return redirect(url_for("index"))

        error = "Неверный логин или пароль"
        record_login_failure(client_ip)

    return render_template(
        "login.html",
        error=error,
        next=request.args.get("next", ""),
    )


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


def load_users():
    if not USERS_FILE.exists():
        return {}

    with USERS_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_users(users):
    _atomic_save_json(USERS_FILE, users)
    with _subscription_cache_lock:
        _subscription_cache.clear()


def load_trash():
    if not TRASH_FILE.exists():
        return {}

    with TRASH_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_trash(trash):
    _atomic_save_json(TRASH_FILE, trash)


def load_source_health():
    if not SOURCE_HEALTH_FILE.exists():
        return {}
    try:
        with SOURCE_HEALTH_FILE.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def source_key(source):
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def source_host(source):
    try:
        return urlsplit(source).hostname or "Неизвестный источник"
    except ValueError:
        return "Некорректный адрес"


def check_source(source):
    if not valid_source_url(source):
        return {"status": "blocked", "http_status": None, "latency_ms": None}
    hostname = urlsplit(source).hostname or ""
    resolved_ips = _public_source_addresses(hostname)
    if not resolved_ips:
        return {"status": "blocked", "http_status": None, "latency_ms": None, "resolved_ips": []}

    started = time.perf_counter()
    try:
        with requests.get(
            source,
            timeout=(2, 4),
            allow_redirects=False,
            stream=True,
            headers={"Accept": "text/plain"},
        ) as response:
            http_status = response.status_code
            next(response.iter_content(1), b"")
        return {
            "status": "ok" if 200 <= http_status < 300 else "error",
            "http_status": http_status,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "resolved_ips": resolved_ips,
        }
    except requests.RequestException:
        return {
            "status": "error",
            "http_status": None,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "resolved_ips": resolved_ips,
        }


def _atomic_save_json(path, value):
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_name, 0o640)
        os.replace(temp_name, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


@app.route("/")
def index():
    host = (request.host or "").split(":", 1)[0].lower()
    if host in {"linebase.ru", "www.linebase.ru"}:
        return redirect(
            "https://youtu.be/FzAZwn2fu2w?si=FD_mOWIa0dSH8vIZ",
            code=302,
        )

    users = load_users()

    items = []

    for token, data in users.items():
        items.append(
            {
                "token": token,
                "name": data.get("name", token[:8]),
                "enabled": data.get("enabled", False),
                "sources": data.get("sources", []),
                "public_sources": data.get("public_sources", []),
                "source_count": len(data.get("sources", []) or []) + len(data.get("public_sources", []) or []),
                "favorite": data.get("favorite", False),
            }
        )

    return render_template(
        "index.html",
        users=items,
        subscription_base_url=SUBSCRIPTION_BASE_URL,
    )


@app.route("/dashboard")
def dashboard():
    users = load_users()
    health = load_source_health()
    source_hosts = {}
    for data in users.values():
        items = list(data.get("sources", []) or []) + list(data.get("public_sources", []) or [])
        if not isinstance(items, list):
            continue
        for source in items:
            if isinstance(source, str):
                key = source_key(source)
                source_hosts.setdefault(
                    key,
                    {
                        "key": key,
                        "host": source_host(source),
                        "health": health.get(key, {}),
                    },
                )
    source_items = list(source_hosts.values())
    healthy = sum(item["health"].get("status") == "ok" for item in source_items)
    favorites = [
        {"name": data.get("name", token[:8]), "token": token}
        for token, data in users.items()
        if data.get("favorite", False)
    ][:6]
    return render_template(
        "dashboard.html",
        total_users=len(users),
        active_users=sum(bool(data.get("enabled", False)) for data in users.values()),
        source_count=len(source_items),
        healthy_sources=healthy,
        sources=source_items[:6],
        favorite_users=favorites,
    )


@app.route("/sources")
def sources_page():
    users = load_users()
    health = load_source_health()
    records = {}
    resolved_ip_cache = {}
    for data in users.values():
        name = data.get("name", "Пользователь")
        sources = list(data.get("sources", []) or []) + list(data.get("public_sources", []) or [])
        if not isinstance(sources, list):
            continue
        for source in sources:
            if not isinstance(source, str):
                continue
            key = source_key(source)
            if key not in resolved_ip_cache:
                source_health = health.get(key, {})
                cached_ips = source_health.get("resolved_ips") if isinstance(source_health, dict) else None
                resolved_ip_cache[key] = cached_ips if isinstance(cached_ips, list) else (
                    _public_source_addresses(source_host(source))
                    if valid_source_url(source) else []
                )
            record = records.setdefault(
                key,
                {
                    "key": key,
                    "host": source_host(source),
                    "ips": resolved_ip_cache[key],
                    "users": [],
                    "health": health.get(key, {}),
                },
            )
            if name not in record["users"]:
                record["users"].append(name)
    return render_template("sources.html", sources=list(records.values()))


@app.route("/sources/check", methods=["POST"])
@serialized_state_change
def check_all_sources():
    users = load_users()
    unique_sources = set()
    for data in users.values():
        items = list(data.get("sources", []) or []) + list(data.get("public_sources", []) or [])
        if isinstance(items, list):
            unique_sources.update(item for item in items if isinstance(item, str))
    source_list = sorted(unique_sources)
    if len(source_list) > 40:
        return {"error": "За один запуск можно проверить не более 40 уникальных источников."}, 400

    results = {}
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(check_source, source): source
            for source in source_list
        }
        for future in as_completed(futures):
            source = futures[future]
            result = future.result()
            result["checked_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            results[source_key(source)] = result

    health = load_source_health()
    health.update(results)
    _atomic_save_json(SOURCE_HEALTH_FILE, health)
    return {"checked": len(results), "results": results}, 200


@app.route("/logs")
def logs_page():
    import re
    from urllib.parse import urlparse

    log_file = LOG_FILE
    lines = []
    log_error = None

    try:
        if log_file.is_file():
            with log_file.open("r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-1000:]
        else:
            log_error = f"Файл журнала не найден: {log_file}"
    except OSError as exc:
        log_error = f"Не удалось прочитать журнал: {exc}"

    users = load_users()
    user_names = {
        token[:8]: data.get("name", token[:8])
        for token, data in users.items()
    }

    source_names = {}
    for token, data in users.items():
        key = token[:8]
        source_names[key] = []
        all_sources = list(data.get("sources", []) or []) + list(data.get("public_sources", []) or [])
        for source in all_sources:
            if isinstance(source, str):
                value = source.strip()
            elif isinstance(source, dict):
                value = str(source.get("url", "")).strip()
            else:
                value = ""

            parsed = urlparse(value if "://" in value else "//" + value) if value else None
            source_names[key].append(
                parsed.hostname if parsed and parsed.hostname else "Неизвестный источник"
            )

    events = []
    current = None
    pending_http = None

    log_pattern = re.compile(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+)\s+\[([A-Z]+)\]\s+(.*)$"
    )
    request_pattern = re.compile(r"\brequest user=(\S+)\s+sources=(\d+)")
    result_pattern = re.compile(
        r"\bresult user=(\S+)\s+connections=(\d+)\s+errors=(\d+)"
    )
    http_pattern = re.compile(
        r"\bsource\s+(OK|FAILED|ERROR|TIMEOUT)\s+status=(\d+)"
        r"(?:\s+time=([\d.]+s?))?(?:\s+size=(\d+))?",
        re.IGNORECASE
    )
    source_pattern = re.compile(
        r"\bsource=(\d+)\s+([A-Z_]+)\b"
        r"(?:\s+host=(\S+))?"
        r"(?:\s+connections=(\d+))?(?:\s+time=([\d.]+s?))?",
        re.IGNORECASE
    )

    def finish_group(group):
        if group is None:
            return

        failed_sources = sum(
            1 for src in group["sources"]
            if src["outcome"] not in ("OK", "UNKNOWN")
            or (src["status"] is not None and src["status"] >= 400)
        )

        errors = group["errors"]
        if errors == 0 and failed_sources:
            errors = failed_sources
            group["errors"] = errors

        if errors:
            group["level"] = "error"
        elif group["completed"]:
            group["level"] = "success"
        else:
            group["level"] = "warning"

        group["kind"] = "request"
        group["has_sources"] = bool(group["sources"])
        group["has_result"] = bool(group["completed"])
        group["search"] = " ".join([
            group["user_key"],
            group["user_name"],
            str(group["expected_sources"]),
            str(group["connections"]),
            str(group["errors"]),
            " ".join(
                f'{src["index"]} {src["name"]} {src["outcome"]} '
                f'{src["status"] or ""} {src["connections"] or ""}'
                for src in group["sources"]
            ),
            " ".join(group["raw_lines"]),
        ])

        events.append(group)

    for raw in lines:
        text = raw.rstrip("\r\n")
        match = log_pattern.match(text)

        if match:
            timestamp, log_level, message = match.groups()
            try:
                parsed_timestamp = datetime.strptime(
                    timestamp, "%Y-%m-%d %H:%M:%S,%f"
                ).replace(tzinfo=timezone.utc)
                timestamp = parsed_timestamp.astimezone(
                    ZoneInfo("Asia/Krasnoyarsk")
                ).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
            except ValueError:
                pass
        else:
            timestamp, log_level, message = "", "INFO", text

        request_match = request_pattern.search(message)
        if request_match:
            finish_group(current)
            user_key, expected = request_match.groups()
            current = {
                "timestamp": timestamp,
                "log_level": log_level,
                "user_key": user_key,
                "user_name": user_names.get(user_key, user_key),
                "expected_sources": int(expected),
                "sources": [],
                "connections": 0,
                "errors": 0,
                "completed": False,
                "raw_lines": [text],
                "level": "warning",
                "kind": "request",
                "search": "",
            }
            pending_http = None
            continue

        result = result_pattern.search(message)
        if result:
            result_user, connections, errors = result.groups()

            if current is not None:
                current["raw_lines"].append(text)
                current["connections"] = int(connections)
                current["errors"] = int(errors)
                current["completed"] = True
                current["has_result"] = True
                finish_group(current)
                current = None
            else:
                events.append({
                    "timestamp": timestamp,
                    "log_level": log_level,
                    "user_key": result_user,
                    "user_name": user_names.get(result_user, result_user),
                    "expected_sources": 0,
                    "sources": [],
                    "connections": int(connections),
                    "errors": int(errors),
                    "completed": True,
                    "raw_lines": [text],
                    "level": "error" if int(errors) else "success",
                    "kind": "other",
                    "search": text,
                })
            pending_http = None
            continue

        if current is not None:
            current["raw_lines"].append(text)

            http = http_pattern.search(message)
            if http:
                outcome, status, duration, size = http.groups()
                pending_http = {
                    "outcome": outcome.upper(),
                    "status": int(status),
                    "http_time": duration or "",
                    "size": size or "",
                }
                continue

            source = source_pattern.search(message)
            if source:
                index, outcome, logged_host, connections, duration = source.groups()
                index = int(index)
                names = source_names.get(current["user_key"], [])
                name = logged_host or (
                    names[index - 1] if 0 < index <= len(names) else f"Источник {index}"
                )

                http_data = pending_http or {}
                current["sources"].append({
                    "index": index,
                    "name": name,
                    "outcome": outcome.upper(),
                    "status": http_data.get("status"),
                    "http_time": http_data.get("http_time", ""),
                    "size": http_data.get("size", ""),
                    "connections": int(connections) if connections else 0,
                    "time": duration or "",
                })
                pending_http = None
            continue

        # Оставляем прочие записи журнала доступными для поиска и фильтрации.
        events.append({
            "timestamp": timestamp,
            "log_level": log_level,
            "user_key": "",
            "user_name": "",
            "expected_sources": 0,
            "sources": [],
            "connections": 0,
            "errors": 0,
            "completed": False,
            "raw_lines": [text],
            "level": (
                "error" if log_level in ("ERROR", "CRITICAL")
                else "warning" if log_level == "WARNING"
                else "success"
            ),
            "kind": "other",
            "search": text,
        })

    finish_group(current)

    if request.args.get("fragment") == "1":
        return render_template("log-events.html", events=events)

    return render_template(
        "logs.html",
        lines=lines,
        events=events,
        user_names=user_names,
        source_names=source_names,
        log_error=log_error,
        log_path=str(log_file),
    )

@app.route("/health")
def health():
    return "OK", 200


@app.route("/nav-count")
def nav_count():
    response = Response(
        json.dumps({"users": len(load_users())}),
        status=200,
        content_type="application/json; charset=utf-8",
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/s/<token>")
def subscription(token):
    user = load_users().get(token)
    if not user or not user.get("enabled", False):
        abort(404)
    sources = user.get("sources", [])
    public_sources = user.get("public_sources", [])
    sources = sources if isinstance(sources, list) else []
    public_sources = public_sources if isinstance(public_sources, list) else []
    combined_sources = sources + public_sources
    connections, diagnostics, cached = get_user_connections(
        token,
        combined_sources,
        include_diagnostics=True,
        source_kinds=["personal"] * len(sources) + ["public"] * len(public_sources),
    )
    log_subscription_request(token, diagnostics, len(connections), cached)
    # Send partial results when at least one source worked. This lets clients
    # refresh from healthy sources while an unavailable source is skipped.
    # If none worked, fail the request so Happ won't replace a profile with an
    # empty list; the unavailable sources will be retried on the next refresh.
    if combined_sources and not connections:
        response = Response(
            "No subscription sources are currently available. Retry later.\n",
            status=503,
            content_type="text/plain; charset=utf-8",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["Retry-After"] = "60"
        return response
    encoded = base64.b64encode("\n".join(connections).encode("utf-8")).decode("ascii")
    response = Response(encoded, status=200, content_type="text/plain; charset=utf-8")
    response.headers["Cache-Control"] = "public, max-age=60"
    response.headers["profile-title"] = SUBSCRIPTION_TITLE
    response.headers["profile-update-interval"] = str(SUBSCRIPTION_UPDATE_INTERVAL_HOURS)
    response.headers["announce"] = "base64:" + base64.b64encode(
        SUBSCRIPTION_INFO_TEXT.encode("utf-8")
    ).decode("ascii")
    return response


@app.route("/user/new")
def new_user_page():
    return render_template("new_user.html")


@app.route("/user/create", methods=["POST"])
@serialized_state_change
def create_user():
    users = load_users()

    name = request.form.get("name", "").strip()

    if not name or len(name) > 100:
        return redirect(url_for("new_user_page"))

    token = secrets.token_hex(24)

    while token in users:
        token = secrets.token_hex(24)

    users[token] = {
        "name": name,
        "enabled": True,
        "sources": [],
    }

    save_users(users)

    return redirect(url_for("user_page", token=token))



@app.route("/user/<token>")
def user_page(token):
    users = load_users()

    if token not in users:
        abort(404)

    data = users[token]

    user = {
        "token": token,
        "name": data.get("name", token[:8]),
        "enabled": data.get("enabled", False),
        "sources": data.get("sources", []),
        "public_sources": data.get("public_sources", []),
        "favorite": data.get("favorite", False),
    }

    return render_template(
        "user.html",
        user=user,
        subscription_base_url=SUBSCRIPTION_BASE_URL,
        public_vpn_feeds=PUBLIC_VPN_FEEDS,
    )


@app.route("/user/<token>/favorite", methods=["POST"])
@serialized_state_change
def toggle_user_favorite(token):
    users = load_users()
    if token not in users:
        abort(404)
    users[token]["favorite"] = not bool(users[token].get("favorite", False))
    save_users(users)
    return redirect(url_for("index"))


@app.route("/user/<token>/settings", methods=["POST"])
@serialized_state_change
def update_user(token):
    users = load_users()

    if token not in users:
        abort(404)

    name = request.form.get("name", "").strip()
    enabled = request.form.get("enabled") == "1"

    if name and len(name) <= 100:
        users[token]["name"] = name

    users[token]["enabled"] = enabled

    save_users(users)

    return redirect(url_for("user_page", token=token))


@app.route("/user/<token>/delete", methods=["POST"])
@serialized_state_change
def delete_user(token):
    users = load_users()

    if token not in users:
        abort(404)

    trash = load_trash()

    trash[token] = users.pop(token)

    save_users(users)
    save_trash(trash)

    return redirect(url_for("index"))


@app.route("/trash")
def trash_page():
    trash = load_trash()

    items = []

    for token, data in trash.items():
        items.append(
            {
                "token": token,
                "name": data.get("name", token[:8]),
                "enabled": data.get("enabled", False),
                "sources": data.get("sources", []),
            }
        )

    return render_template("trash.html", users=items)


@app.route("/trash/clear", methods=["POST"])
@serialized_state_change
def clear_trash():
    save_trash({})
    return redirect(url_for("trash_page"))


@app.route("/trash/<token>/restore", methods=["POST"])
@serialized_state_change
def restore_user(token):
    users = load_users()
    trash = load_trash()

    if token not in trash:
        abort(404)

    if token in users:
        abort(409)

    users[token] = trash.pop(token)

    save_users(users)
    save_trash(trash)

    return redirect(url_for("trash_page"))


@app.route("/user/<token>/sources", methods=["POST"])
@serialized_state_change
def add_source(token):
    users = load_users()

    if token not in users:
        abort(404)

    source = request.form.get("source", "").strip()

    if not source:
        return redirect(url_for("user_page", token=token))
    if not valid_source_url(source):
        abort(400, description="Источник должен быть корректным URL с http:// или https://.")

    users[token].setdefault("sources", [])

    if source not in users[token]["sources"]:
        users[token]["sources"].append(source)
        save_users(users)

    return redirect(url_for("user_page", token=token))


@app.route("/user/<token>/public-sources/add", methods=["POST"], defaults={"feed_id": "top150"})
@app.route("/user/<token>/public-sources/add/<feed_id>", methods=["POST"])
@serialized_state_change
def add_public_source(token, feed_id):
    users = load_users()
    if token not in users:
        abort(404)
    feed = PUBLIC_VPN_FEEDS.get(feed_id)
    if not feed:
        abort(404)
    sources = users[token].setdefault("public_sources", [])
    if feed["url"] not in sources:
        sources.append(feed["url"])
        save_users(users)
    return redirect(url_for("user_page", token=token))


@app.route("/user/<token>/public-sources/<int:index>/delete", methods=["POST"])
@serialized_state_change
def delete_public_source(token, index):
    users = load_users()
    if token not in users:
        abort(404)
    sources = users[token].setdefault("public_sources", [])
    if 0 <= index < len(sources):
        sources.pop(index)
        save_users(users)
    return redirect(url_for("user_page", token=token))


@app.route("/user/<token>/public-sources/<int:index>/update", methods=["POST"])
@serialized_state_change
def update_public_source(token, index):
    users = load_users()
    if token not in users:
        abort(404)

    sources = users[token].setdefault("public_sources", [])
    if not 0 <= index < len(sources):
        abort(404)

    source = request.form.get("source", "").strip()
    if not valid_source_url(source):
        abort(400, description="Укажите корректный публичный URL с http:// или https:// (до 2048 символов).")

    if source not in sources or sources[index] != source:
        sources[index] = source
        save_users(users)

    return redirect(url_for("user_page", token=token))


@app.route("/user/<token>/sources/<int:index>/delete", methods=["POST"])
@serialized_state_change
def delete_source(token, index):
    users = load_users()

    if token not in users:
        abort(404)

    sources = users[token].setdefault("sources", [])

    if 0 <= index < len(sources):
        sources.pop(index)
        save_users(users)

    return redirect(url_for("user_page", token=token))




@app.route("/user/<token>/sources/reorder", methods=["POST"])
@serialized_state_change
def reorder_sources(token):
    users = load_users()

    if token not in users:
        abort(404)

    try:
        new_order = request.get_json(force=True)
    except Exception:
        abort(400)

    if not isinstance(new_order, list) or not all(isinstance(item, str) for item in new_order):
        abort(400)

    sources = users[token].setdefault("sources", [])

    # Проверяем количество
    if len(new_order) != len(sources):
        abort(400)

    # Проверяем, что переданы именно существующие источники
    if not all(isinstance(item, str) for item in sources) or set(new_order) != set(sources):
        abort(400)

    # Сохраняем новый порядок
    users[token]["sources"] = new_order
    save_users(users)

    return {"success": True}, 200


@app.route("/bulk")
def bulk_page():
    users = load_users()

    items = []

    for token, data in users.items():
        items.append({
            "token": token,
            "name": data.get("name", token[:8]),
            "enabled": data.get("enabled", False),
            "sources": data.get("sources", []) or [],
            "public_sources": data.get("public_sources", []) or [],
        })

    return render_template("bulk.html", users=items)


@app.route("/bulk/sources/delete", methods=["POST"])
@serialized_state_change
def bulk_delete_sources():
    users = load_users()

    selected = request.form.getlist("source")

    by_user = {}

    for item in selected:
        if "|" not in item:
            continue

        parts = item.rsplit("|", 2)
        if len(parts) == 2:
            token, index_text = parts
            source_kind = "personal"
        else:
            token, source_kind, index_text = parts

        try:
            index = int(index_text)
        except ValueError:
            continue

        if token in users:
            if source_kind not in {"personal", "public"}:
                continue
            by_user.setdefault(token, []).append((source_kind, index))

    removed = 0
    affected_names = []

    for token, selections in by_user.items():
        changed = False
        for source_kind, indexes in (("personal", []), ("public", [])):
            indexes.extend(index for kind, index in selections if kind == source_kind)
            key = "sources" if source_kind == "personal" else "public_sources"
            sources = users[token].setdefault(key, [])
            valid_indexes = sorted({i for i in indexes if 0 <= i < len(sources)}, reverse=True)
            for index in valid_indexes:
                sources.pop(index)
                removed += 1
                changed = True

        if not changed:
            continue

        affected_names.append(
            users[token].get("name", token[:8])
        )

    if removed:
        save_users(users)

    return render_template(
        "bulk-result.html",
        removed=removed,
        affected_users=len(affected_names),
        affected_names=affected_names,
    )


@app.route("/bulk/sources/add", methods=["POST"])
@serialized_state_change
def bulk_add_sources():
    users = load_users()

    selected_tokens = set(request.form.getlist("user_token"))
    raw_sources = request.form.get("sources", "")

    sources_to_add = list(dict.fromkeys(
        line.strip()
        for line in raw_sources.splitlines()
        if line.strip() and valid_source_url(line.strip())
    ))
    skipped_invalid = sum(
        1 for line in raw_sources.splitlines()
        if line.strip() and not valid_source_url(line.strip())
    )

    if not selected_tokens or not sources_to_add:
        return redirect(url_for("bulk_page"))

    added = 0
    skipped = skipped_invalid * len(selected_tokens)
    affected_names = []

    for token in selected_tokens:
        if token not in users:
            continue

        current = users[token].setdefault("sources", [])
        if current is None:
            current = users[token]["sources"] = []

        user_added = 0

        for source in sources_to_add:
            if source in current:
                skipped += 1
                continue

            current.append(source)
            added += 1
            user_added += 1

        if user_added:
            affected_names.append(
                users[token].get("name", token[:8])
            )

    if added:
        save_users(users)

    return render_template(
        "bulk-add-result.html",
        added=added,
        skipped=skipped,
        affected_users=len(affected_names),
        affected_names=affected_names,
    )


if __name__ == "__main__":
    app.run(
        host="127.0.0.1",
        port=int(os.environ.get("PANEL_PORT", "18082")),
        debug=False,
    )
