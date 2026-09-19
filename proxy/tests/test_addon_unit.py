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
