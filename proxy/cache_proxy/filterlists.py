"""Download and refresh the category block lists.

    python -m cache_proxy.filterlists update [--source NAME]
    python -m cache_proxy.filterlists status

Sources are declared in config.toml under [filtering] (see there for the
format). Each source's list for each enforced category is normalised to one
domain per line and stored at

    <lists_dir>/<category>/<source>.txt   readable, one domain per line
    <lists_dir>/<category>/<source>.idx   compact hash index the proxy loads

so the proxy can union everything in a category directory, and an admin can
see at a glance where a block came from (and grep the .txt). Nothing is
written into /etc.

A refresh never makes things worse: the download goes to a temporary file
which replaces the live list atomically, and only if it parsed to a sane
number of domains -- an empty or badly truncated download keeps yesterday's
list and is reported as an error.
"""
import argparse
import io
import json
import logging
import os
import sys
import tarfile
import time
import urllib.request
from pathlib import Path
from typing import Optional

from cache_proxy import categories, config
from cache_proxy.contentfilter import build_index, parse_host_lines

logger = logging.getLogger("cache_proxy.filterlists")

MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
FETCH_TIMEOUT = 120
# A new list smaller than this fraction of the one it replaces is treated as
# a broken download, not a genuine clean-up.
MIN_SHRINK_RATIO = 0.5
STATUS_FILE = "status.json"


def _fetch(url: str, headers: Optional[dict] = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "cache-proxy-filterlists/1", **(headers or {})})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        data = resp.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError(f"download larger than {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB")
    return data


def _auth_headers(source: dict) -> dict:
    env = source.get("auth_key_env")
    key = os.environ.get(env, "") if env else ""
    return {"Auth-Key": key} if key else {}


def _extract_ut1(data: bytes, category: str) -> str:
    """The UT1 tarball for a category holds <category>/domains (large
    categories may split it into domains.0, domains.1, ...). The directory
    name is checked: UT1's malware.tar.gz has been seen to contain the
    phishing list, and mislabelling it would be worse than an error."""
    parts = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for m in tar.getmembers():
            directory, _, name = m.name.rpartition("/")
            if m.isfile() and directory == category and (name == "domains" or name.startswith("domains.")):
                parts.append(tar.extractfile(m).read().decode("utf-8", errors="replace"))
    if not parts:
        raise ValueError(f"tarball has no {category}/domains (upstream mislabelled it?)")
    return "\n".join(parts)


def _read_count(path: Path) -> int:
    """Domains in the existing list, from its index (8 bytes each)."""
    try:
        return path.with_suffix(".idx").stat().st_size // 8
    except OSError:
        return 0


def _atomic_write(target: Path, data: bytes) -> None:
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)


def _write_list(category: str, source: str, hosts: set) -> int:
    target = config.FILTER_LISTS_DIR / category / f"{source}.txt"
    if not hosts:
        raise ValueError("download contained no domains")
    index = build_index(hosts)
    new, old = len(index) // 8, _read_count(target)
    if old and new < old * MIN_SHRINK_RATIO:
        raise ValueError(f"new list has {new} domains, previous had {old}; keeping the previous list")
    target.parent.mkdir(parents=True, exist_ok=True)
    # The proxy loads the .idx, so it is written last: a reader never sees an
    # index newer than the text it was built from.
    _atomic_write(target, ("\n".join(sorted(hosts)) + "\n").encode("utf-8"))
    _atomic_write(target.with_suffix(".idx"), index)
    return new


def _jobs(only_source: Optional[str], frequency: Optional[str] = None, only_categories: Optional[list] = None):
    """(source name, category, url, kind, headers) for everything enforced.
    frequency="hourly" selects only sources marked refresh = "hourly"; None
    selects every source (what the daily run does)."""
    enforced = set(config.block_categories())
    if only_categories is not None:
        enforced &= set(only_categories)
    for src in config.FILTER_SOURCES:
        if only_source and src["name"] != only_source:
            continue
        if frequency and src.get("refresh", "daily") != frequency:
            continue
        for cat in categories.source_categories(src):
            if cat in enforced:
                yield src["name"], cat, src["url"].format(category=cat), src["type"], _auth_headers(src)


def update_source(name: str, category: str, url: str, kind: str, headers: dict) -> dict:
    result = {"source": name, "category": category, "ts": time.time(), "count": None, "error": None}
    try:
        data = _fetch(url, headers)
        if kind == "ut1":
            text = _extract_ut1(data, category)
        elif kind in ("domains", "hosts"):
            text = data.decode("utf-8", errors="replace")
        else:
            raise ValueError(f"unknown source type {kind!r}")
        # parse_host_lines handles both plain domain lists and hosts-file lines.
        result["count"] = _write_list(category, name, parse_host_lines(text))
    except Exception as e:  # keep going: one bad source must not stop the rest
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def read_status() -> dict:
    try:
        return json.loads((config.FILTER_LISTS_DIR / STATUS_FILE).read_text())
    except (OSError, ValueError):
        return {}


def _write_status(results: list) -> None:
    status = read_status()
    for r in results:
        key = f"{r['source']}/{r['category']}"
        prev = status.get(key, {})
        if r["error"]:
            # Keep the time/count of the last good download, record the failure.
            status[key] = {**prev, "last_error": r["error"], "last_error_ts": r["ts"]}
        else:
            status[key] = {"ts": r["ts"], "count": r["count"], "last_error": None}
    config.FILTER_LISTS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.FILTER_LISTS_DIR / (STATUS_FILE + ".tmp")
    tmp.write_text(json.dumps(status, indent=1))
    os.replace(tmp, config.FILTER_LISTS_DIR / STATUS_FILE)


def update_all(only_source: Optional[str] = None, frequency: Optional[str] = None,
               only_categories: Optional[list] = None) -> list:
    results = []
    for job in _jobs(only_source, frequency, only_categories):
        r = update_source(*job)
        logger.info("%s/%s: %s", r["source"], r["category"], r["error"] or f"{r['count']} domains")
        results.append(r)
    if results:
        _write_status(results)
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cache_proxy.filterlists")
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("update", help="download/refresh the block lists")
    up.add_argument("--source", help="only this source")
    up.add_argument("--frequency", choices=["hourly"],
                    help="only sources marked refresh = \"hourly\" (used by the hourly timer)")
    sub.add_parser("status", help="show what was last downloaded")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.cmd == "status":
        for key, v in sorted(read_status().items()):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(v["ts"])) if v.get("ts") else "never"
            print(f"{key:35} {v.get('count') or 0:>9} domains  updated {when}"
                  + (f"  LAST ERROR: {v['last_error']}" if v.get("last_error") else ""))
        return 0

    results = update_all(args.source, args.frequency)
    if not results:
        print("nothing to update (no source matches an enforced category"
              + (f" and refresh = \"{args.frequency}\")" if args.frequency else ")"), file=sys.stderr)
        # Nothing scheduled hourly is a valid configuration, not a failure.
        return 0 if args.frequency else 1
    return 1 if any(r["error"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
