#!/usr/bin/env python3
"""Show safe public DNS addresses beside each monitored subscription source."""
from pathlib import Path
import sys


ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/multisub-panel")


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected one {label} marker, found {count}")
    return text.replace(old, new, 1)


def read(path: Path) -> str:
    with path.open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def write(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as stream:
        stream.write(text)


def patch_panel(text: str) -> str:
    old_resolver = '''def _public_source_host(hostname):
    try:
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError):
        return False
    return bool(addresses) and all(address.is_global for address in addresses)'''
    new_resolver = '''def _public_source_addresses(hostname):
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
    return bool(_public_source_addresses(hostname))'''
    text = replace_once(text, old_resolver, new_resolver, "public DNS resolver")

    old_check = '''    if not _public_source_host(hostname):
        return {"status": "blocked", "http_status": None, "latency_ms": None}

    started = time.perf_counter()'''
    new_check = '''    resolved_ips = _public_source_addresses(hostname)
    if not resolved_ips:
        return {"status": "blocked", "http_status": None, "latency_ms": None, "resolved_ips": []}

    started = time.perf_counter()'''
    text = replace_once(text, old_check, new_check, "source checker")
    text = replace_once(
        text,
        '''            "latency_ms": round((time.perf_counter() - started) * 1000),
        }
    except requests.RequestException:''',
        '''            "latency_ms": round((time.perf_counter() - started) * 1000),
            "resolved_ips": resolved_ips,
        }
    except requests.RequestException:''',
        "successful source result",
    )
    text = replace_once(
        text,
        '''            "http_status": None,
            "latency_ms": round((time.perf_counter() - started) * 1000),
        }


def _atomic_save_json''',
        '''            "http_status": None,
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "resolved_ips": resolved_ips,
        }


def _atomic_save_json''',
        "failed source result",
    )

    start = text.index('def sources_page():')
    end = text.index('@app.route("/sources/check"', start)
    section = text[start:end]
    section = replace_once(
        section,
        '''    records = {}
    for data in users.values():''',
        '''    records = {}
    resolved_ip_cache = {}
    for data in users.values():''',
        "source page cache",
    )
    section = replace_once(
        section,
        '''            key = source_key(source)
            record = records.setdefault(''',
        '''            key = source_key(source)
            if key not in resolved_ip_cache:
                source_health = health.get(key, {})
                cached_ips = source_health.get("resolved_ips") if isinstance(source_health, dict) else None
                resolved_ip_cache[key] = cached_ips if isinstance(cached_ips, list) else (
                    _public_source_addresses(source_host(source))
                    if valid_source_url(source) else []
                )
            record = records.setdefault(''',
        "source IP lookup",
    )
    section = replace_once(
        section,
        '''                    "host": source_host(source),
                    "users": [],''',
        '''                    "host": source_host(source),
                    "ips": resolved_ip_cache[key],
                    "users": [],''',
        "source IP template data",
    )
    return text[:start] + section + text[end:]


def patch_template(text: str) -> str:
    text = replace_once(
        text,
        '<div class="host">{{ source.host }}</div>',
        '<div class="host">{{ source.host }}</div><div class="source-ip">IP: <span>{% if source.ips %}{{ source.ips|join(", ") }}{% else %}не определён{% endif %}</span></div>',
        "source host row",
    )
    text = replace_once(
        text,
        "http.textContent='HTTP '+(result.http_status??'—');",
        "http.textContent='HTTP '+(result.http_status??'—');const ip=row.querySelector('.source-ip span');if(ip)ip.textContent=(result.resolved_ips||[]).join(', ')||'не определён';",
        "source check IP refresh",
    )
    text = text.replace(
        "filename='panel-cosmic.css', v=3",
        "filename='panel-cosmic.css', v=4",
    )
    return text


def main() -> int:
    panel_path = ROOT / "app" / "panel.py"
    template_path = ROOT / "templates" / "sources.html"
    panel = patch_panel(read(panel_path))
    template = patch_template(read(template_path))
    write(panel_path, panel)
    write(template_path, template)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
