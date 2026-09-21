"""Content filtering: decide whether a request should be blocked.

Two kinds of input, kept in separate places:

Downloaded category lists  <lists_dir>/<category>/<source>.idx, refreshed by
                           cache_proxy.filterlists. Only the categories named
                           in [filtering] block_categories are enforced.

Hand-edited overrides in /etc/cache-proxy/ (one entry per line, "#" comments):
  blocked-hosts.conf         extra hosts (and subdomains) to block
  allowed-hosts.conf         hosts (and subdomains) never blocked -- beats
                             every other rule. This is where approved
                             unblock requests go.
  blocked-url-patterns.conf  case-insensitive regexes searched in the full URL

Precedence: allowed > blocked-hosts > category list > URL pattern.

Host matching walks the request host's parent domains, so a million-domain
list costs about the same per request as a short one. Downloaded lists are
kept as sorted arrays of 64-bit domain hashes (<source>.idx, 8 bytes per
domain, built by cache_proxy.filterlists) that every proxy worker
memory-maps: 5 million domains cost ~40 MB of page cache shared by all
workers, not ~500 MB in each. The readable <source>.txt next to it is for
admins (grep it to see whether a domain is listed). A hash collision (~1 in
10^12 per lookup) would block an innocent domain, and the allow-list fixes
that. Everything is re-read when a file changes, and lists are swapped in
whole.

TLS caveat: only traffic the proxy decrypts can be filtered. Hosts in
never-intercept-hosts.conf are tunnelled raw and are invisible to this module.
"""
import hashlib
import logging
import mmap
import os
import re
from array import array
from bisect import bisect_left
from html import escape
from pathlib import Path
from typing import NamedTuple, Optional

from cache_proxy import config

logger = logging.getLogger("cache_proxy")

_IP_RE = re.compile(r"^(\d{1,3}(\.\d{1,3}){3}|[0-9a-fA-F]*:[0-9a-fA-F:.]+)$")
_HOSTS_FILE_SINKS = {"localhost", "localhost.localdomain", "local", "broadcasthost", "ip6-localhost", "ip6-loopback"}


class Verdict(NamedTuple):
    kind: str       # "category" | "host" | "url"
    category: str   # category name for kind == "category", else ""
    source: str     # which list it came from ("" for url/host rules)
    detail: str     # the matched host or pattern

    @property
    def reason(self) -> str:
        if self.kind == "category":
            return f"Category not allowed: {self.category}"
        if self.kind == "host":
            return "Site blocked by the administrator"
        return "URL matches a blocked pattern"


def _clean_host(h: str) -> str:
    return h.lower().lstrip("*.").rstrip(".")


def parse_host_lines(text: str) -> set:
    hosts = set()
    for line in text.splitlines():
        parts = line.split("#", 1)[0].split()
        if not parts:
            continue
        # "0.0.0.0 example.com [alias ...]" -> take the names, skip the IP.
        names = parts[1:] if len(parts) > 1 and _IP_RE.match(parts[0]) else parts[:1]
        for n in names:
            n = _clean_host(n)
            if n and n not in _HOSTS_FILE_SINKS:
                hosts.add(n)
    return hosts


def parse_patterns(text: str) -> list:
    compiled = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            compiled.append(re.compile(line, re.IGNORECASE))
        except re.error as e:
            logger.warning("ignoring bad blocked-url-pattern %r: %s", line, e)
    return compiled


def _parents(host: str):
    """host, then each parent domain: a.b.example.com -> b.example.com -> example.com -> com."""
    host = _clean_host(host)
    if _IP_RE.match(host):  # an IP literal has no parent domains
        yield host
        return
    while host:
        yield host
        _, _, host = host.partition(".")


def host_hash(host: str) -> int:
    return int.from_bytes(hashlib.blake2b(host.encode(), digest_size=8).digest(), "little")


def build_index(hosts) -> bytes:
    """Sorted, de-duplicated 64-bit hashes of the hosts, packed for HashIndex."""
    return array("Q", sorted({host_hash(h) for h in hosts})).tobytes()


class HashIndex:
    """Read-only, memory-mapped view of a build_index() file."""

    def __init__(self, path: Path):
        self._file = open(path, "rb")
        size = os.fstat(self._file.fileno()).st_size
        if size % 8:
            self._file.close()
            raise ValueError(f"{path}: size {size} is not a multiple of 8")
        self._mm = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ) if size else None
        self._view = memoryview(self._mm).cast("Q") if size else ()

    def __len__(self) -> int:
        return len(self._view)

    def contains_hash(self, h: int) -> bool:
        i = bisect_left(self._view, h)
        return i < len(self._view) and self._view[i] == h


def _stat_sig(path: Path):
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


class ContentFilter:
    def __init__(self, blocked_file: Path = None, allowed_file: Path = None, patterns_file: Path = None,
                 lists_dir: Path = None, categories: list = None):
        self._blocked_file = blocked_file or config.BLOCKED_HOSTS_FILE
        self._allowed_file = allowed_file or config.ALLOWED_HOSTS_FILE
        self._patterns_file = patterns_file or config.BLOCKED_URL_PATTERNS_FILE
        self._lists_dir = lists_dir or config.FILTER_LISTS_DIR
        self._categories = [c.lower() for c in (config.FILTER_BLOCK_CATEGORIES if categories is None else categories)]
        self._sig = None
        self.blocked: set = set()
        self.allowed: set = set()
        self.patterns: list = []
        self.indexes: list = []          # [(HashIndex, category, source)]
        self.reload_if_changed()

    def _signature(self):
        sig = [_stat_sig(self._blocked_file), _stat_sig(self._allowed_file), _stat_sig(self._patterns_file)]
        for cat in self._categories:
            try:
                files = sorted((self._lists_dir / cat).glob("*.idx"))
            except OSError:
                files = []
            sig.extend((str(f), _stat_sig(f)) for f in files)
        return tuple(sig)

    @staticmethod
    def _read(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def reload_if_changed(self) -> bool:
        """Re-read anything that changed. Returns True if it reloaded.
        The new state is built completely before being swapped in; a lookup
        already running keeps using the list it started with, and the old
        mappings are freed once nothing references them."""
        sig = self._signature()
        if sig == self._sig:
            return False
        self._sig = sig
        blocked = parse_host_lines(self._read(self._blocked_file))
        allowed = parse_host_lines(self._read(self._allowed_file))
        patterns = parse_patterns(self._read(self._patterns_file))
        indexes = []
        for cat in self._categories:
            for f in sorted((self._lists_dir / cat).glob("*.idx")):
                try:
                    indexes.append((HashIndex(f), cat, f.stem))
                except (OSError, ValueError) as e:
                    logger.warning("skipping unreadable block list %s: %s", f, e)
        self.blocked, self.allowed, self.patterns, self.indexes = blocked, allowed, patterns, indexes
        logger.info(
            "content filter loaded: %d category domains (%s), %d blocked hosts, %d allowed hosts, %d url patterns",
            sum(len(i) for i, _, _ in indexes), ", ".join(self._categories) or "no categories",
            len(blocked), len(allowed), len(patterns),
        )
        return True

    def check(self, host: str, url: str) -> Optional[Verdict]:
        parents = list(_parents(host))
        if any(p in self.allowed for p in parents):
            return None
        for p in parents:
            if p in self.blocked:
                return Verdict("host", "", "", p)
        indexes = self.indexes
        if indexes:
            for p in parents:
                h = host_hash(p)
                for index, cat, src in indexes:
                    if index.contains_hash(h):
                        return Verdict("category", cat, src, p)
        for rx in self.patterns:
            if rx.search(url):
                return Verdict("url", "", "", rx.pattern)
        return None


def block_page(message: str, host: str, reason: str) -> bytes:
    """Minimal inline page, used when no block page service is configured."""
    return (
        '<!doctype html><meta charset="utf-8"><title>Blocked</title>'
        '<body style="font-family:sans-serif;max-width:32rem;margin:15vh auto;padding:0 1rem">'
        f"<h1>Access blocked</h1><p>{escape(message)}</p>"
        f'<p style="color:#666">{escape(reason)}<br>Requested host: {escape(host)}</p></body>'
    ).encode()
