"""Runtime config, loaded from /etc/cache-proxy/config.toml (or
CACHE_PROXY_CONFIG) with built-in defaults as a fallback, so the app still
runs with no config file present (tests, Docker, a fresh checkout).

Host exclusion lists live in their own flat files (one hostname pattern
per line, "#" for comments) rather than as TOML arrays -- easier to hand
to ops than editing TOML syntax, and greppable/diffable/scriptable.

Environment variables (CACHE_PROXY_DIR, etc.) still override specific
values on top of the config file -- kept for the test suite and
compose.yml, which set an ephemeral cache dir per run.
"""
import os
import re
import tomllib
from pathlib import Path

CONFIG_PATH = Path(os.environ.get("CACHE_PROXY_CONFIG", "/etc/cache-proxy/config.toml"))
NEVER_CACHE_HOSTS_FILE = Path(
    os.environ.get("CACHE_PROXY_NEVER_CACHE_FILE", CONFIG_PATH.parent / "never-cache-hosts.conf")
)
NEVER_INTERCEPT_HOSTS_FILE = Path(
    os.environ.get("CACHE_PROXY_NEVER_INTERCEPT_FILE", CONFIG_PATH.parent / "never-intercept-hosts.conf")
)

_DEFAULTS = {
    "cache": {
        "dir": "/var/lib/cache-proxy/files",
        "db": "/var/lib/cache-proxy/index.db",
        "min_size_mb": 1,
        "max_buffer_size_mb": 512,
        # How long a cached download is served before it's refetched.
        "download_ttl_days": 30,
        # Total cache size cap in GB; 0 = unlimited. Least-recently-hit
        # entries are evicted first once the cap is exceeded.
        "max_size_gb": 0,
        # File extensions worth caching (software installers, packages).
        "extensions": [
            ".exe", ".msi", ".msix", ".msixbundle", ".cab", ".appx",
            ".deb", ".rpm", ".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".zip",
            ".iso", ".img", ".dmg", ".pkg", ".apk",
        ],
        # Content-Type fallback for URLs without a recognizable extension.
        "content_types": [
            "application/octet-stream",
            "application/x-msdownload",
            "application/vnd.microsoft.portable-executable",
            "application/x-debian-package",
            "application/x-rpm",
            "application/x-redhat-package-manager",
            "application/zip",
            "application/gzip",
            "application/x-xz",
            "application/x-bzip2",
            "application/x-iso9660-image",
            "application/vnd.android.package-archive",
        ],
    },
    # Short-lived cache for static web assets (never HTML).
    "webcache": {
        "enabled": True,
        "ttl_minutes": 60,
        "max_size_mb": 5,
        "extensions": [
            ".js", ".mjs", ".css", ".png", ".jpg", ".jpeg", ".gif", ".webp",
            ".svg", ".ico", ".woff", ".woff2", ".ttf", ".otf",
        ],
        "content_types": [
            "text/css", "application/javascript", "text/javascript",
            "image/png", "image/jpeg", "image/gif", "image/webp",
            "image/svg+xml", "image/x-icon", "image/vnd.microsoft.icon",
            "font/woff", "font/woff2", "font/ttf", "font/otf",
        ],
    },
    "proxy": {
        "min_workers": 2,
        "max_workers": 0,  # 0 = number of CPUs
        "scale_up_cpu_pct": 70,
        "scale_down_cpu_pct": 20,
        "scale_down_idle_seconds": 300,
        "sample_interval_seconds": 5,
        "status_file": "/var/lib/cache-proxy/workers.json",
    },
    "analytics": {
        "anomaly_factor": 3.0,
        "min_history_hours": 6,
        "lookback_hours": 168,
        "retention_days": 90,
    },
    "webui": {
        "port": 443,
        "username": "",
        "password_hash": "",
        "tls_cert": "/etc/cache-proxy/webui-cert.pem",
        "tls_key": "/etc/cache-proxy/webui-key.pem",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load() -> dict:
    cfg = _DEFAULTS
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            cfg = _deep_merge(_DEFAULTS, tomllib.load(f))
    return cfg


_cfg = _load()

CACHE_DIR = Path(os.environ.get("CACHE_PROXY_DIR", _cfg["cache"]["dir"]))
DB_PATH = Path(os.environ.get("CACHE_PROXY_DB", _cfg["cache"]["db"]))

# Skip tiny responses (favicons, redirects, JSON manifests that happen to
# carry a cacheable content-type) so the cache only holds real downloads.
MIN_CACHE_SIZE = int(os.environ.get("CACHE_PROXY_MIN_SIZE", _cfg["cache"]["min_size_mb"] * 1024 * 1024))

# mitmproxy buffers a response body fully in memory before any addon hook
# sees it, unless the flow is marked to stream. We only want to pay that
# cost for things we're actually going to cache -- everything else (web
# pages, images, video, SaaS traffic) streams straight through. This also
# caps how large a single candidate we'll buffer, so a multi-GB ISO with a
# cacheable extension doesn't get held entirely in RAM.
MAX_BUFFER_SIZE = int(os.environ.get("CACHE_PROXY_MAX_BUFFER_SIZE", _cfg["cache"]["max_buffer_size_mb"] * 1024 * 1024))

DOWNLOAD_TTL = int(float(os.environ.get("CACHE_PROXY_DOWNLOAD_TTL_DAYS", _cfg["cache"]["download_ttl_days"])) * 86400)
MAX_CACHE_BYTES = int(float(os.environ.get("CACHE_PROXY_MAX_SIZE_GB", _cfg["cache"]["max_size_gb"])) * 1024 ** 3)

WEBCACHE_ENABLED = bool(_cfg["webcache"]["enabled"])
WEBCACHE_TTL = int(float(os.environ.get("CACHE_PROXY_WEBCACHE_TTL_MINUTES", _cfg["webcache"]["ttl_minutes"])) * 60)
WEBCACHE_MAX_SIZE = int(_cfg["webcache"]["max_size_mb"] * 1024 * 1024)
WEBCACHE_EXTENSIONS = set(_cfg["webcache"]["extensions"])
WEBCACHE_CONTENT_TYPES = set(_cfg["webcache"]["content_types"])

MIN_WORKERS = max(1, int(os.environ.get("CACHE_PROXY_MIN_WORKERS", _cfg["proxy"]["min_workers"])))
MAX_WORKERS = int(os.environ.get("CACHE_PROXY_MAX_WORKERS", _cfg["proxy"]["max_workers"])) or (os.cpu_count() or 1)
MAX_WORKERS = max(MIN_WORKERS, MAX_WORKERS)
SCALE_UP_CPU_PCT = float(_cfg["proxy"]["scale_up_cpu_pct"])
SCALE_DOWN_CPU_PCT = float(_cfg["proxy"]["scale_down_cpu_pct"])
SCALE_DOWN_IDLE_SECONDS = float(os.environ.get("CACHE_PROXY_SCALE_DOWN_IDLE_SECONDS", _cfg["proxy"]["scale_down_idle_seconds"]))
SAMPLE_INTERVAL = float(os.environ.get("CACHE_PROXY_SAMPLE_INTERVAL", _cfg["proxy"]["sample_interval_seconds"]))
WORKER_STATUS_FILE = Path(os.environ.get("CACHE_PROXY_STATUS_FILE", _cfg["proxy"]["status_file"]))

ANOMALY_FACTOR = float(_cfg["analytics"]["anomaly_factor"])
MIN_HISTORY_HOURS = int(_cfg["analytics"]["min_history_hours"])
LOOKBACK_HOURS = int(_cfg["analytics"]["lookback_hours"])
RETENTION_DAYS = int(_cfg["analytics"]["retention_days"])

CACHEABLE_EXTENSIONS = set(_cfg["cache"]["extensions"])
CACHEABLE_CONTENT_TYPES = set(_cfg["cache"]["content_types"])

WEBUI_PORT = int(os.environ.get("CACHE_PROXY_WEBUI_PORT", _cfg["webui"]["port"]))
WEBUI_USERNAME = os.environ.get("CACHE_PROXY_WEBUI_USERNAME", _cfg["webui"]["username"])
WEBUI_PASSWORD_HASH = os.environ.get("CACHE_PROXY_WEBUI_PASSWORD_HASH", _cfg["webui"]["password_hash"])
WEBUI_TLS_CERT = Path(os.environ.get("CACHE_PROXY_WEBUI_TLS_CERT", _cfg["webui"]["tls_cert"]))
WEBUI_TLS_KEY = Path(os.environ.get("CACHE_PROXY_WEBUI_TLS_KEY", _cfg["webui"]["tls_key"]))


def _load_host_list(path: Path) -> list:
    if not path.exists():
        return []
    hosts = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            hosts.append(line)
    return hosts


NEVER_CACHE_HOSTS = _load_host_list(NEVER_CACHE_HOSTS_FILE)
NEVER_INTERCEPT_HOSTS = _load_host_list(NEVER_INTERCEPT_HOSTS_FILE)


def host_matches(hostname: str, patterns: list) -> bool:
    """True if hostname equals a pattern or is a subdomain of one.
    Patterns may optionally be written as "*.example.com" -- the "*."
    is stripped and treated the same as "example.com" (both match the
    bare domain and any subdomain)."""
    if not hostname or not patterns:
        return False
    hostname = hostname.lower().rstrip(".")
    for pattern in patterns:
        p = pattern.lower().lstrip("*.").rstrip(".")
        if hostname == p or hostname.endswith("." + p):
            return True
    return False


def host_to_regex(pattern: str) -> str:
    """Convert a "example.com" / "*.example.com" host pattern into an
    anchored regex matching the bare host or any subdomain -- for handing
    to mitmproxy's ignore_hosts option, which takes regexes rather than
    the simple patterns host_matches() understands."""
    p = pattern.lower().lstrip("*.").rstrip(".")
    return rf"(^|\.){re.escape(p)}$"
