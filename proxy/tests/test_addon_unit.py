"""Fast unit tests (no mitmdump subprocess) for the in-memory cache-key
set that keeps request() from hitting SQLite on every GET through the
office's general browsing traffic.
"""
from unittest.mock import MagicMock, patch

import pytest

from cache_proxy import addon, config, store


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    store.init_db()


def _fake_flow(url: str, method: str = "GET") -> MagicMock:
    flow = MagicMock()
    flow.request.method = method
    flow.request.pretty_url = url
    flow.request.host = url.split("://", 1)[1].split("/", 1)[0].split(":")[0]
    flow.request.headers = {}
    flow.client_conn.peername = ("10.0.0.5", 12345)
    return flow


def test_load_populates_known_hashes_from_store():
    store.save_file("https://example.com/a.exe", "a.exe", "application/x-msdownload", b"x" * 10)
    a = addon.CacheAddon()
    a.load(loader=None)
    assert a._known_hashes == {store.url_hash("https://example.com/a.exe")}


def test_request_skips_db_lookup_for_unknown_url():
    a = addon.CacheAddon()
    a.load(loader=None)
    flow = _fake_flow("https://example.com/never-cached.js")
    original_response = flow.response

    with patch.object(store, "get_entry") as mock_get_entry:
        a.request(flow)
        mock_get_entry.assert_not_called()
    assert flow.response is original_response  # untouched -- no HIT was fabricated


def test_request_serves_hit_for_known_url(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    store.init_db()
    store.save_file("https://example.com/a.exe", "a.exe", "application/x-msdownload", b"x" * 10)

    a = addon.CacheAddon()
    a.load(loader=None)
    flow = _fake_flow("https://example.com/a.exe")
    flow.response = None

    a.request(flow)
    assert flow.response is not None
    assert flow.response.headers["X-Cache-Proxy"] == "HIT"


def test_storing_a_new_file_adds_it_to_known_hashes(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    store.init_db()

    a = addon.CacheAddon()
    a.load(loader=None)
    assert a._known_hashes == set()

    flow = _fake_flow("https://example.com/new.msi")
    flow.response.headers = {"Content-Type": "application/x-msdownload"}
    flow.response.status_code = 200
    flow.response.stream = False
    flow.response.content = b"y" * (config.MIN_CACHE_SIZE + 1)
    flow.request.path = "/new.msi"

    a.response(flow)
    assert store.url_hash("https://example.com/new.msi") in a._known_hashes


def test_never_cache_host_is_not_stored(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "NEVER_CACHE_HOSTS", ["internal-crm.example.com"])
    store.init_db()

    a = addon.CacheAddon()
    a.load(loader=None)

    flow = _fake_flow("https://internal-crm.example.com/export.zip")
    flow.response.headers = {"Content-Type": "application/zip"}
    flow.response.status_code = 200
    flow.response.stream = False
    flow.response.content = b"z" * (config.MIN_CACHE_SIZE + 1)
    flow.request.path = "/export.zip"

    a.response(flow)
    assert a._known_hashes == set()
    assert store.list_entries() == []


def test_never_cache_host_matches_subdomain(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "NEVER_CACHE_HOSTS", ["example.com"])
    store.init_db()

    a = addon.CacheAddon()
    a.load(loader=None)

    flow = _fake_flow("https://downloads.example.com/tool.deb")
    flow.response.headers = {"Content-Type": "application/x-debian-package"}
    flow.response.status_code = 200
    flow.response.stream = False
    flow.response.content = b"z" * (config.MIN_CACHE_SIZE + 1)
    flow.request.path = "/tool.deb"

    a.response(flow)
    assert store.list_entries() == []


def test_ordinary_host_still_caches_when_exclusion_list_nonempty(tmp_path, monkeypatch):
    """The exclusion check shouldn't accidentally swallow everything."""
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "NEVER_CACHE_HOSTS", ["internal-crm.example.com"])
    store.init_db()

    a = addon.CacheAddon()
    a.load(loader=None)

    flow = _fake_flow("https://downloads.example.com/tool.deb")
    flow.response.headers = {"Content-Type": "application/x-debian-package"}
    flow.response.status_code = 200
    flow.response.stream = False
    flow.response.content = b"z" * (config.MIN_CACHE_SIZE + 1)
    flow.request.path = "/tool.deb"

    a.response(flow)
    assert len(store.list_entries()) == 1


def test_small_download_is_skipped_by_min_size(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    store.init_db()

    a = addon.CacheAddon()
    a.load(loader=None)

    flow = _fake_flow("https://example.com/small.exe")
    flow.response.headers = {"Content-Type": "application/x-msdownload"}
    flow.response.status_code = 200
    flow.response.stream = False
    flow.response.content = b"x" * (config.MIN_CACHE_SIZE - 1)
    flow.request.path = "/small.exe"

    a.response(flow)
    assert store.list_entries() == []


def test_small_deb_is_cached_despite_min_size(tmp_path, monkeypatch):
    """apt upgrades are mostly small .deb files -- these must not be
    filtered out by the same floor that skips favicons/redirects."""
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    store.init_db()

    a = addon.CacheAddon()
    a.load(loader=None)

    flow = _fake_flow("https://gb.archive.ubuntu.com/ubuntu/pool/main/h/htop/htop.deb")
    flow.response.headers = {"Content-Type": "application/x-debian-package"}
    flow.response.status_code = 200
    flow.response.stream = False
    flow.response.content = b"x" * (config.MIN_CACHE_SIZE - 1)
    flow.request.path = "/ubuntu/pool/main/h/htop/htop.deb"

    a.response(flow)
    assert len(store.list_entries()) == 1


def test_small_rpm_is_cached_despite_min_size(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    store.init_db()

    a = addon.CacheAddon()
    a.load(loader=None)

    flow = _fake_flow("https://mirror.example.com/pkgs/tool.rpm")
    flow.response.headers = {"Content-Type": "application/x-rpm"}
    flow.response.status_code = 200
    flow.response.stream = False
    flow.response.content = b"x" * (config.MIN_CACHE_SIZE - 1)
    flow.request.path = "/pkgs/tool.rpm"

    a.response(flow)
    assert len(store.list_entries()) == 1


def test_small_deb_still_respects_other_exclusions(tmp_path, monkeypatch):
    """The min_size exemption must not bypass never-cache-hosts."""
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "NEVER_CACHE_HOSTS", ["internal-crm.example.com"])
    store.init_db()

    a = addon.CacheAddon()
    a.load(loader=None)

    flow = _fake_flow("https://internal-crm.example.com/tool.deb")
    flow.response.headers = {"Content-Type": "application/x-debian-package"}
    flow.response.status_code = 200
    flow.response.stream = False
    flow.response.content = b"x" * (config.MIN_CACHE_SIZE - 1)
    flow.request.path = "/tool.deb"

    a.response(flow)
    assert store.list_entries() == []

# --- hourly traffic metering ----------------------------------------------
# _bump_hourly() meters every response, including the streamed ones that
# access_log never sees. It must never raise: metering may not break a
# response.

def _hourly_addon():
    a = addon.CacheAddon.__new__(addon.CacheAddon)
    a._hourly = {}
    return a


def _resp(headers=None, content=b"", stream=False):
    r = MagicMock()
    r.headers = headers or {}
    r.content = content
    r.stream = stream
    return r


def test_bump_hourly_uses_content_length():
    a = _hourly_addon()
    flow = _fake_flow("https://example.com/x")
    flow.response = _resp({"content-length": "1234"})

    a._bump_hourly(flow)
    (ip, _hour), tally = next(iter(a._hourly.items()))
    assert ip == "10.0.0.5"
    assert tally == [1, 1234]


def test_bump_hourly_falls_back_to_buffered_body():
    a = _hourly_addon()
    flow = _fake_flow("https://example.com/x")
    flow.response = _resp({}, content=b"abcde", stream=False)

    a._bump_hourly(flow)
    assert next(iter(a._hourly.values())) == [1, 5]


def test_bump_hourly_does_not_read_the_body_of_a_streamed_response():
    """A streamed response was never buffered; reading .content can raise.
    The request must still be counted, with zero bytes rather than a guess."""
    a = _hourly_addon()
    flow = _fake_flow("https://example.com/x")
    resp = MagicMock()
    resp.headers = {}
    resp.stream = True
    type(resp).content = property(lambda self: (_ for _ in ()).throw(ValueError("streamed")))
    flow.response = resp

    a._bump_hourly(flow)  # must not raise
    assert next(iter(a._hourly.values())) == [1, 0]


def test_bump_hourly_swallows_unexpected_errors():
    a = _hourly_addon()
    flow = _fake_flow("https://example.com/x")
    resp = MagicMock()
    type(resp).headers = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    flow.response = resp

    a._bump_hourly(flow)  # must not raise
    assert a._hourly == {}


def test_bump_hourly_sums_within_the_same_hour():
    a = _hourly_addon()
    for n in ("100", "250"):
        flow = _fake_flow("https://example.com/x")
        flow.response = _resp({"content-length": n})
        a._bump_hourly(flow)

    assert len(a._hourly) == 1
    assert next(iter(a._hourly.values())) == [2, 350]


# --- never-intercept host metering (volume only, never content) -----------
# mitmproxy never decrypts these connections. tcp_message must only ever
# read len(message.content), never store or forward the content itself,
# and must clear flow.messages afterwards -- show_ignored_hosts keeps every
# message in memory for the life of the connection otherwise (its own docs
# call this out as a real memory-usage risk).

def _fake_tcp_flow(client_ip="10.0.0.5", messages=None):
    flow = MagicMock()
    flow.client_conn.peername = (client_ip, 12345)
    flow.messages = messages if messages is not None else []
    return flow


def _msg(content: bytes):
    m = MagicMock()
    m.content = content
    return m


def test_tcp_message_tallies_bytes_and_clears_messages():
    a = _hourly_addon()
    flow = _fake_tcp_flow(messages=[_msg(b"x" * 100)])

    a.tcp_message(flow)

    (ip, _hour), tally = next(iter(a._hourly.items()))
    assert ip == "10.0.0.5"
    assert tally == [0, 100]  # tcp_message never counts a "request" itself
    assert flow.messages == []


def test_tcp_message_only_tallies_the_latest_message():
    """flow.messages accumulates the whole connection's history; only the
    newest entry is this event's actual delta."""
    a = _hourly_addon()
    flow = _fake_tcp_flow(messages=[_msg(b"a" * 50), _msg(b"b" * 30)])

    a.tcp_message(flow)

    assert next(iter(a._hourly.values())) == [0, 30]


def test_tcp_message_with_no_messages_is_a_noop():
    a = _hourly_addon()
    flow = _fake_tcp_flow(messages=[])

    a.tcp_message(flow)  # must not raise
    assert a._hourly == {}


def test_tcp_message_swallows_unexpected_errors():
    a = _hourly_addon()
    flow = _fake_tcp_flow()
    bad_msg = MagicMock()
    type(bad_msg).content = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    flow.messages = [bad_msg]

    a.tcp_message(flow)  # must not raise
    assert a._hourly == {}
    assert flow.messages == []  # still cleared -- the tally failed, not the cleanup


def test_tcp_end_counts_the_connection_once():
    a = _hourly_addon()
    flow = _fake_tcp_flow()

    a.tcp_end(flow)

    assert next(iter(a._hourly.values())) == [1, 0]


def test_tcp_error_also_counts_the_connection():
    a = _hourly_addon()
    flow = _fake_tcp_flow()

    a.tcp_error(flow)

    assert next(iter(a._hourly.values())) == [1, 0]


def test_metering_disabled_skips_tcp_hooks(monkeypatch):
    monkeypatch.setattr(config, "METER_IGNORED_HOSTS", False)
    a = _hourly_addon()
    flow = _fake_tcp_flow(messages=[_msg(b"x" * 100)])

    a.tcp_message(flow)
    a.tcp_end(flow)

    assert a._hourly == {}
