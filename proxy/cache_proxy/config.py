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
