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
from functools import wraps
from urllib.parse import urlsplit
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

SUBSCRIPTION_BASE_URL = os.environ.get(
    "SUBSCRIPTION_BASE_URL", ""
).rstrip("/")
SUBSCRIPTION_PROTOCOLS = (
    "vless://", "vmess://", "trojan://", "ss://", "hysteria2://",
    "hy2://", "hysteria://", "tuic://", "wireguard://",
)
_subscription_cache = {}
_subscription_cache_lock = threading.Lock()

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


def _public_source_host(hostname):
    try:
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError):
        return False
    return bool(addresses) and all(address.is_global for address in addresses)


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


def get_user_connections(token, sources):
    now = time.monotonic()
    with _subscription_cache_lock:
        cached = _subscription_cache.get(token)
        if cached and cached[0] > now:
            return list(cached[1])

    connections = []
    seen = set()
    for source in sources[:50]:
        if not valid_source_url(source):
            continue
        hostname = urlsplit(source).hostname
        if not _public_source_host(hostname):
            continue
        try:
            with requests.get(
                source,
                timeout=(3, 8),
                allow_redirects=False,
                stream=True,
                headers={"Accept": "text/plain"},
            ) as response:
                if response.status_code != 200:
                    continue
                payload = bytearray()
                for chunk in response.iter_content(65536):
                    payload.extend(chunk)
                    if len(payload) > 1024 * 1024:
                        payload.clear()
                        break
                if not payload:
                    continue
        except requests.RequestException:
            continue
        for connection in _connections_from_payload(bytes(payload)):
            if connection not in seen:
                seen.add(connection)
                connections.append(connection)
                if len(connections) >= 500:
                    break
        if len(connections) >= 500:
            break

    with _subscription_cache_lock:
        _subscription_cache[token] = (time.monotonic() + 60, tuple(connections))
    return connections


@app.before_request
def require_login():
    public_endpoints = {
        "login",
        "health",
        "static",
        "public_user_page",
        "subscription",
    }

    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        expected = session.get("_csrf_token", "")
        supplied = request.form.get("csrf_token", "") or request.headers.get("X-CSRFToken", "")
        if not expected or not supplied or not hmac.compare_digest(expected, supplied):
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
    if session.get("authenticated"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("authenticated"):
        return redirect(url_for("index"))

    error = None

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        next_url = request.form.get("next", "")
        client_ip = request.remote_addr or "unknown"

        if login_is_blocked(client_ip):
            return render_template("login.html", error="Слишком много попыток. Подожди 15 минут.", next=""), 429

        if (
            secrets.compare_digest(username, PANEL_USERNAME)
            and secrets.compare_digest(password, PANEL_PASSWORD)
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
    users = load_users()

    items = []

    for token, data in users.items():
        items.append(
            {
                "token": token,
                "name": data.get("name", token[:8]),
                "enabled": data.get("enabled", False),
                "sources": data.get("sources", []),
            }
        )

    return render_template(
        "index.html",
        users=items,
        subscription_base_url=SUBSCRIPTION_BASE_URL,
    )


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
        for source in data.get("sources", []):
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
        else:
            timestamp, log_level, message = "", "INFO", text

        request = request_pattern.search(message)
        if request:
            finish_group(current)
            user_key, expected = request.groups()
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
                index, outcome, connections, duration = source.groups()
                index = int(index)
                names = source_names.get(current["user_key"], [])
                name = names[index - 1] if 0 < index <= len(names) else f"Источник {index}"

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


@app.route("/p/<token>")
def public_user_page(token):
    user = load_users().get(token)
    if not user or not user.get("enabled", False):
        abort(404)
    sources = user.get("sources", [])
    connections = get_user_connections(token, sources if isinstance(sources, list) else [])
    subscription_url = (
        f"{SUBSCRIPTION_BASE_URL}/s/{token}"
        if SUBSCRIPTION_BASE_URL
        else url_for("subscription", token=token, _external=True)
    )
    return render_template(
        "public_user.html",
        user={"name": user.get("name", token[:8]), "enabled": True, "sources": sources},
        connections=connections,
        subscription_url=subscription_url,
    )


@app.route("/s/<token>")
def subscription(token):
    user = load_users().get(token)
    if not user or not user.get("enabled", False):
        abort(404)
    sources = user.get("sources", [])
    connections = get_user_connections(token, sources if isinstance(sources, list) else [])
    encoded = base64.b64encode("\n".join(connections).encode("utf-8")).decode("ascii")
    response = Response(encoded, status=200, content_type="text/plain; charset=utf-8")
    response.headers["Cache-Control"] = "public, max-age=60"
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
    }

    return render_template(
        "user.html",
        user=user,
        subscription_base_url=SUBSCRIPTION_BASE_URL,
    )


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

        token, index = item.rsplit("|", 1)

        try:
            index = int(index)
        except ValueError:
            continue

        if token in users:
            by_user.setdefault(token, []).append(index)

    removed = 0
    affected_names = []

    for token, indexes in by_user.items():
        sources = users[token].setdefault("sources", [])

        valid_indexes = sorted(
            {i for i in indexes if 0 <= i < len(sources)},
            reverse=True
        )

        if not valid_indexes:
            continue

        for index in valid_indexes:
            sources.pop(index)
            removed += 1

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
