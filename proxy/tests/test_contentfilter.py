"""Content filter: list parsing, category lists, precedence, live reload,
and the addon's block responses (redirect for page loads, 403 otherwise)."""
import os
from unittest.mock import MagicMock

import pytest

from cache_proxy import addon, config, contentfilter, store


def _write_list(idx_path, text):
    """Write a category list the way cache_proxy.filterlists does."""
    idx_path.parent.mkdir(parents=True, exist_ok=True)
    idx_path.write_bytes(contentfilter.build_index(contentfilter.parse_host_lines(text)))


def _filter(tmp_path, blocked="", allowed="", patterns="", categories=None, lists=None):
    """lists: {"adult/ut1": "a.com\\nb.com"} -> <tmp>/lists/adult/ut1.txt"""
    (tmp_path / "b").write_text(blocked)
    (tmp_path / "a").write_text(allowed)
    (tmp_path / "p").write_text(patterns)
    for name, text in (lists or {}).items():
        _write_list(tmp_path / "lists" / f"{name}.idx", text)
    return contentfilter.ContentFilter(
        tmp_path / "b", tmp_path / "a", tmp_path / "p", tmp_path / "lists",
        categories=categories if categories is not None else ["adult", "malware"],
    )


def test_parse_host_lines_handles_comments_wildcards_hosts_format_and_ips():
    text = "# c\nBad.com\n*.evil.org  # trailing\n0.0.0.0 ads.net tracker.net\n127.0.0.1 localhost\n1.2.3.4\n\n"
    assert contentfilter.parse_host_lines(text) == {"bad.com", "evil.org", "ads.net", "tracker.net", "1.2.3.4"}


def test_ip_literals_can_be_blocked_exactly(tmp_path):
    f = _filter(tmp_path, blocked="10.1.2.3\n")
    assert f.check("10.1.2.3", "http://10.1.2.3/x").kind == "host"
    assert f.check("10.1.2.4", "http://10.1.2.4/") is None
    assert f.check("2.3", "http://2.3/") is None


def test_host_and_subdomain_blocked_but_not_lookalikes(tmp_path):
    f = _filter(tmp_path, blocked="bad.com\n")
    v = f.check("www.a.bad.com", "http://www.a.bad.com/x")
    assert v.kind == "host" and v.detail == "bad.com"
    assert f.check("notbad.com", "http://notbad.com/") is None
    assert f.check("bad.com.evil.io", "http://bad.com.evil.io/") is None


def test_category_list_verdict_names_category_and_source(tmp_path):
    f = _filter(tmp_path, lists={"adult/ut1": "porn.example\n", "malware/urlhaus": "evil.example\n"})
    v = f.check("cdn.porn.example", "http://cdn.porn.example/")
    assert (v.kind, v.category, v.source) == ("category", "adult", "ut1")
    assert v.reason == "Category not allowed: adult"
    assert f.check("evil.example", "http://evil.example/").category == "malware"


def test_only_enforced_categories_are_loaded(tmp_path):
    f = _filter(tmp_path, categories=["adult"], lists={"adult/ut1": "a.example\n", "gambling/ut1": "g.example\n"})
    assert f.check("a.example", "http://a.example/")
    assert f.check("g.example", "http://g.example/") is None


def test_allowlist_beats_manual_block_category_and_patterns(tmp_path):
    f = _filter(tmp_path, blocked="bad.com\n", allowed="ok.bad.com\nporn.example\n",
                patterns="secret\n", lists={"adult/ut1": "porn.example\n"})
    assert f.check("ok.bad.com", "http://ok.bad.com/secret") is None
    assert f.check("porn.example", "http://porn.example/") is None
    assert f.check("x.bad.com", "http://x.bad.com/")


def test_url_pattern_case_insensitive_and_bad_regex_skipped(tmp_path):
    f = _filter(tmp_path, patterns="([unclosed\n" + r"\.torrent(\?|$)" + "\n")
    assert len(f.patterns) == 1
    v = f.check("h.com", "http://h.com/a/FILE.TORRENT")
    assert v.kind == "url"
    assert f.check("h.com", "http://h.com/a/file.zip") is None


def test_reload_picks_up_edits_new_lists_and_missing_files(tmp_path):
    f = _filter(tmp_path, blocked="one.com\n", lists={"adult/ut1": "x.example\n"})
    assert not f.reload_if_changed()
    (tmp_path / "b").write_text("two.com\n")
    os.utime(tmp_path / "b", ns=(1, 1))
    _write_list(tmp_path / "lists" / "adult" / "other.idx", "y.example\n")
    assert f.reload_if_changed()
    assert f.check("one.com", "http://one.com/") is None and f.check("two.com", "http://two.com/")
    assert f.check("y.example", "http://y.example/").source == "other"
    (tmp_path / "b").unlink()
    assert f.reload_if_changed() and f.blocked == set()


def test_index_lookup_is_exact_and_handles_empty_and_corrupt_files(tmp_path):
    f = _filter(tmp_path, lists={"adult/ut1": "".join(f"d{i}.example\n" for i in range(1000))})
    assert f.check("d0.example", "http://d0.example/") and f.check("x.d999.example", "http://x.d999.example/")
    assert f.check("d1000.example", "http://d1000.example/") is None
    assert f.check("example", "http://example/") is None
    (tmp_path / "lists" / "adult" / "empty.idx").write_bytes(b"")
    (tmp_path / "lists" / "adult" / "bad.idx").write_bytes(b"12345")  # not a multiple of 8
    assert f.reload_if_changed()
    assert f.check("d5.example", "http://d5.example/")  # still works; the bad file is skipped


def test_filter_follows_the_saved_category_selection_without_a_restart(tmp_path, monkeypatch):
    cats = tmp_path / "cats.conf"
    cats.write_text("adult\n")
    monkeypatch.setattr(config, "CATEGORIES_FILE", cats)
    for name in ("adult/ut1", "gambling/ut1"):
        _write_list(tmp_path / "lists" / f"{name}.idx", f"{name.split('/')[0]}.example\n")
    f = contentfilter.ContentFilter(tmp_path / "b", tmp_path / "a", tmp_path / "p", tmp_path / "lists")
    assert f.check("adult.example", "http://adult.example/") and f.check("gambling.example", "http://gambling.example/") is None
    cats.write_text("gambling\n")  # same size, possibly same mtime: must still be noticed
    assert f.reload_if_changed()
    assert f.check("adult.example", "http://adult.example/") is None and f.check("gambling.example", "http://gambling.example/")
    cats.write_text("")
    assert f.reload_if_changed() and f.check("gambling.example", "http://gambling.example/") is None


def test_inline_block_page_escapes_html():
    assert b"<script>" not in contentfilter.block_page("<script>x</script>", "<b>h</b>", "<i>r</i>")


# ---- addon behaviour ---------------------------------------------------------

@pytest.fixture
def blocking_addon(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "files")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "FILTERING_ENABLED", True)
    monkeypatch.setattr(config, "BLOCKED_HOSTS_FILE", tmp_path / "b")
    monkeypatch.setattr(config, "ALLOWED_HOSTS_FILE", tmp_path / "a")
    monkeypatch.setattr(config, "BLOCKED_URL_PATTERNS_FILE", tmp_path / "p")
    monkeypatch.setattr(config, "FILTER_LISTS_DIR", tmp_path / "lists")
    monkeypatch.setattr(config, "CATEGORIES_FILE", tmp_path / "no-such-categories.conf")
    monkeypatch.setattr(config, "FILTER_BLOCK_CATEGORIES", ["adult"])
    monkeypatch.setattr(config, "BLOCKPAGE_URL", "")
    (tmp_path / "b").write_text("bad.com\n")
    _write_list(tmp_path / "lists" / "adult" / "ut1.idx", "porn.example\n")
    store.init_db()
    return addon.CacheAddon()


def _flow(url, dest=None, method="GET", ua="TestBrowser/1.0"):
    flow = MagicMock()
    flow.request.pretty_url = url
    flow.request.method = method
    flow.request.host = url.split("://", 1)[1].split("/", 1)[0]
    flow.request.headers = {"User-Agent": ua}
    if dest:
        flow.request.headers["Sec-Fetch-Dest"] = dest
    flow.client_conn.peername = ("10.0.0.5", 1)
    return flow


def test_no_blockpage_configured_gives_inline_403_and_is_recorded_on_flush(blocking_addon):
    flow = _flow("https://bad.com/setup.exe", dest="document")
    assert blocking_addon._blocked(flow) is True
    resp = flow.response
    assert resp.status_code == 403 and b"Access blocked" in resp.content
    assert resp.headers["X-Cache-Proxy"] == "BLOCKED"
    assert blocking_addon._counters["blocked_requests"] == 1
    blocking_addon._flush_blocks()
    (row,) = store.list_blocked()
    assert (row["client_ip"], row["host"], row["kind"], row["user_agent"]) == ("10.0.0.5", "bad.com", "host", "TestBrowser/1.0")


def test_page_load_redirects_to_block_page_with_a_token_row(blocking_addon, monkeypatch):
    monkeypatch.setattr(config, "BLOCKPAGE_URL", "http://proxy.lan")
    flow = _flow("https://porn.example/x?y=1", dest="document")
    assert blocking_addon._blocked(flow)
    resp = flow.response
    assert resp.status_code == 302 and resp.headers["X-Block-Category"] == "adult"
    token = resp.headers["Location"].split("t=", 1)[1]
    assert resp.headers["Location"].startswith("http://proxy.lan/blocked?t=")
    row = store.get_block(token)  # written immediately, so the page can read it
    assert (row["category"], row["source"], row["url"]) == ("adult", "ut1", "https://porn.example/x?y=1")


def test_non_page_requests_get_plain_403_not_a_redirect(blocking_addon, monkeypatch):
    monkeypatch.setattr(config, "BLOCKPAGE_URL", "http://proxy.lan")
    for flow in (_flow("https://bad.com/a.js", dest="script"), _flow("https://bad.com/pkg.deb"),
                 _flow("https://bad.com/", dest="document", method="POST")):
        assert blocking_addon._blocked(flow)
        assert flow.response.status_code == 403 and "Location" not in flow.response.headers
    assert len(blocking_addon._pending_blocks) == 3
    assert store.list_blocked() == []          # queued, not yet flushed
    blocking_addon._flush_blocks()
    assert len(store.list_blocked()) == 3


def test_block_page_requests_are_routed_to_loopback_and_tagged(blocking_addon, monkeypatch):
    monkeypatch.setattr(config, "BLOCKPAGE_URL", "http://proxy.lan")
    monkeypatch.setattr(config, "BLOCKPAGE_PORT", 8099)
    a = addon.CacheAddon()
    flow = _flow("http://proxy.lan/blocked?t=abc")
    flow.request.headers["X-Client-IP"] = "6.6.6.6"  # a forged value must be replaced
    assert a._to_block_page(flow) is True
    assert flow.request.headers["X-Client-IP"] == "10.0.0.5"
    assert (flow.request.host, flow.request.port) == ("127.0.0.1", 8099)
    assert a._to_block_page(_flow("http://elsewhere.com/")) is False


def test_unblocked_traffic_passes(blocking_addon):
    assert blocking_addon._blocked(_flow("https://fine.com/setup.exe", dest="document")) is False


def test_filter_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "index.db")
    monkeypatch.setattr(config, "FILTERING_ENABLED", False)
    assert addon.CacheAddon()._blocked(_flow("https://bad.com/")) is False
