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
BLOCKED_HOSTS_FILE = Path(
    os.environ.get("CACHE_PROXY_BLOCKED_HOSTS_FILE", CONFIG_PATH.parent / "blocked-hosts.conf")
)
ALLOWED_HOSTS_FILE = Path(
    os.environ.get("CACHE_PROXY_ALLOWED_HOSTS_FILE", CONFIG_PATH.parent / "allowed-hosts.conf")
)
CATEGORIES_FILE = Path(
    os.environ.get("CACHE_PROXY_CATEGORIES_FILE", CONFIG_PATH.parent / "filter-categories.conf")
)
BLOCKED_URL_PATTERNS_FILE = Path(
    os.environ.get("CACHE_PROXY_BLOCKED_URL_PATTERNS_FILE", CONFIG_PATH.parent / "blocked-url-patterns.conf")
)

_DEFAULTS = {
    "cache": {
        "dir": "/var/lib/cache-proxy/files",
        "db": "/var/lib/cache-proxy/index.db",
        "min_size_mb": 1,
        # Package-manager files (Linux distro updates) are cached regardless
        # of min_size_mb -- a 50 KB .deb is still a real, repeatable,
        # worth-deduplicating download, not the kind of tiny fragment
        # min_size_mb exists to filter out.
        "no_min_size_extensions": [".deb", ".udeb", ".rpm"],
        "max_buffer_size_mb": 512,
        # How long a cached download is served before it's refetched.
        "download_ttl_days": 30,
        # When several clients ask for the same not-yet-cached file at once,
        # the first fetches it and the rest wait up to this long, then are
        # served from the cache. 0 disables (every request goes upstream).
        "coalesce_wait_seconds": 60,
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
        # Volume-only metering (connection count + byte count, per client per
        # hour) for hosts in never-intercept-hosts.conf. mitmproxy never
        # decrypts these connections; this reads the length of each
        # encrypted TCP message, never its content, and turns on
        # mitmproxy's own show_ignored_hosts option to get a flow object
        # for them at all -- that option's own docs warn it holds each
        # ignored flow's messages in memory, so the addon must clear them
        # after tallying (see CacheAddon.tcp_message).
        "meter_ignored_hosts": True,
    },
    # Content filtering. Downloaded category lists live under lists_dir (kept
    # apart from /etc); the small hand-edited override files stay in /etc.
    "filtering": {
        "enabled": False,
        "lists_dir": "/var/lib/cache-proxy/filter-lists",
        # Categories that are enforced. Only these are downloaded.
        "block_categories": ["adult", "gambling", "malware", "phishing"],
        "sources": [
            {
                "name": "ut1",
                "type": "ut1",
                "url": "https://dsi.ut-capitole.fr/blacklists/download/{category}.tar.gz",
                # No "categories" key: offers every UT1 category, and only the
                # ones you enable (Categories page) are downloaded.
            },
            {
                # Their malware tarball currently contains the phishing list,
                # so take this one category from the GitHub mirror instead.
                "name": "ut1-malware",
                "type": "domains",
                "url": "https://raw.githubusercontent.com/olbat/ut1-blacklists/master/blacklists/malware/domains",
                "category": "malware",
            },
            {
                "name": "phishing-database",
                "type": "domains",
                "url": "https://raw.githubusercontent.com/mitchellkrogza/Phishing.Database/master/phishing-domains-ACTIVE.txt",
                "category": "phishing",
                "refresh": "hourly",
            },
            {
                "name": "urlhaus",
                "type": "hosts",
                "url": "https://urlhaus.abuse.ch/downloads/hostfile/",
                "category": "malware",
                "auth_key_env": "CACHE_PROXY_URLHAUS_KEY",
                "refresh": "hourly",
            },
        ],
    },
    # Root/intermediate CAs mitmproxy should trust for the *upstream*
    # (real-server) TLS connection, beyond certifi's bundle -- for vendor
    # PKI hierarchies not in the public web root program (Windows Update's
    # is the first one found). See vendorcas.py.
    "tls_trust": {
        "enabled": True,
        "dir": "/var/lib/cache-proxy/vendor-cas",
        "seed_hosts": [
            "fe2cr.update.microsoft.com",
            "tas02.sls.update.microsoft.com",
        ],
        "max_chain_depth": 6,
    },
    # The page blocked users are sent to (a separate small service).
    "blockpage": {
        "url": "",  # e.g. "http://cache-proxy.example.lan"; empty = inline 403 page
        "port": 80,
        "message": "This site is blocked by your organisation's web policy.",
        "max_requests_per_ip_per_hour": 5,
    },
    # Outgoing mail for unblock requests. The password is NOT stored here:
    # set CACHE_PROXY_SMTP_PASSWORD in /etc/cache-proxy/secrets.env.
    "email": {
        "smtp_host": "",
        "smtp_port": 587,
        "security": "starttls",  # starttls | ssl | none
        "username": "",
        "from_address": "",
        "unblock_recipient": "",
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

COALESCE_WAIT = float(os.environ.get("CACHE_PROXY_COALESCE_WAIT", _cfg["cache"]["coalesce_wait_seconds"]))

WEBCACHE_ENABLED =bool(_cfg["webcache"]["enabled"])
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
METER_IGNORED_HOSTS = os.environ.get(
    "CACHE_PROXY_METER_IGNORED_HOSTS", str(_cfg["analytics"]["meter_ignored_hosts"])
).lower() in ("1", "true", "yes")

CACHEABLE_EXTENSIONS = set(_cfg["cache"]["extensions"])
CACHEABLE_CONTENT_TYPES = set(_cfg["cache"]["content_types"])
NO_MIN_SIZE_EXTENSIONS = set(_cfg["cache"]["no_min_size_extensions"])

FILTERING_ENABLED = os.environ.get("CACHE_PROXY_FILTERING", str(_cfg["filtering"]["enabled"])).lower() in ("1", "true", "yes")
FILTER_LISTS_DIR = Path(os.environ.get("CACHE_PROXY_LISTS_DIR", _cfg["filtering"]["lists_dir"]))
FILTER_BLOCK_CATEGORIES = [c.lower() for c in _cfg["filtering"]["block_categories"]]  # default; see block_categories()


def parse_categories_text(text: str) -> list:
    """Category names from a filter-categories.conf body (one per line, "#"
    comments), lower-cased, de-duplicated, invalid names dropped."""
    out = []
    for line in text.splitlines():
        name = line.split("#", 1)[0].strip().lower()
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name) and name not in out:
            out.append(name)
    return out


def block_categories() -> list:
    """The categories to enforce right now. The web UI's Categories page
    writes filter-categories.conf; if that file exists it decides (an empty
    one means none), otherwise the default in config.toml applies. Read on
    every call so a saved change needs no restart."""
    try:
        return parse_categories_text(CATEGORIES_FILE.read_text(encoding="utf-8"))
    except OSError:
        return list(FILTER_BLOCK_CATEGORIES)
FILTER_SOURCES = list(_cfg["filtering"]["sources"])

TLS_TRUST_ENABLED = os.environ.get("CACHE_PROXY_TLS_TRUST", str(_cfg["tls_trust"]["enabled"])).lower() in ("1", "true", "yes")
VENDOR_CA_DIR = Path(os.environ.get("CACHE_PROXY_VENDOR_CA_DIR", _cfg["tls_trust"]["dir"]))
VENDOR_CA_SEED_HOSTS = list(_cfg["tls_trust"]["seed_hosts"])
VENDOR_CA_MAX_DEPTH = int(_cfg["tls_trust"]["max_chain_depth"])

BLOCKPAGE_URL = os.environ.get("CACHE_PROXY_BLOCKPAGE_URL", _cfg["blockpage"]["url"]).rstrip("/")
BLOCKPAGE_PORT = int(os.environ.get("CACHE_PROXY_BLOCKPAGE_PORT", _cfg["blockpage"]["port"]))
BLOCKPAGE_MESSAGE = str(_cfg["blockpage"]["message"])
UNBLOCK_MAX_PER_IP_HOUR = int(os.environ.get("CACHE_PROXY_UNBLOCK_MAX_PER_HOUR", _cfg["blockpage"]["max_requests_per_ip_per_hour"]))

SMTP_HOST = os.environ.get("CACHE_PROXY_SMTP_HOST", _cfg["email"]["smtp_host"])
SMTP_PORT = int(os.environ.get("CACHE_PROXY_SMTP_PORT", _cfg["email"]["smtp_port"]))
SMTP_SECURITY = os.environ.get("CACHE_PROXY_SMTP_SECURITY", _cfg["email"]["security"]).lower()
SMTP_USERNAME = _cfg["email"]["username"]
SMTP_PASSWORD = os.environ.get("CACHE_PROXY_SMTP_PASSWORD", "")
EMAIL_FROM = os.environ.get("CACHE_PROXY_EMAIL_FROM", _cfg["email"]["from_address"])
UNBLOCK_RECIPIENT = os.environ.get("CACHE_PROXY_UNBLOCK_RECIPIENT", _cfg["email"]["unblock_recipient"])

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
    the simple patterns host_matches() understands.

    The trailing port is optional because mitmproxy matches ignore_hosts
    against "host:port", not the bare hostname. Without it the anchor
    never matches a real request and the whole never-intercept list is
    silently ignored for HTTPS -- which is the only traffic it exists
    for."""
    p = pattern.lower().lstrip("*.").rstrip(".")
    return rf"(^|\.){re.escape(p)}(:\d+)?$"
