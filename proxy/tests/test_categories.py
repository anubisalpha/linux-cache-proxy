"""Categories: the catalogue, the saved selection, and the admin page that
edits it (real web UI server; a local HTTP server stands in for the list
providers so nothing touches the internet)."""
import http.server
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from cache_proxy import categories, config
from cache_proxy.webui.auth import hash_password

from .test_blockpage import PROXY_ROOT, _auth, _free_port, _req, _serve, _wait_for  # noqa: F401


# ---- catalogue ---------------------------------------------------------------

def test_available_uses_configured_sources_and_ut1_default_list():
    by_name = {c["name"]: c for c in categories.available()}
    assert by_name["adult"]["sources"] == ["ut1"]
    assert by_name["adult"]["group"] == "Adult"
    # UT1's own malware tarball is unusable, so malware comes only from the other feeds.
    assert "ut1" not in by_name["malware"]["sources"] and "ut1-malware" in by_name["malware"]["sources"]
    assert {"ut1", "phishing-database"} <= set(by_name["phishing"]["sources"])
    assert len(by_name) > 50
    # aliases and non-blocking lists are not offered
    for excluded in ("porn", "drugs", "child", "liste_blanche", "examen_pix", "tricheur_pix", "special"):
        assert excluded not in by_name


def test_sources_you_add_appear_under_other(monkeypatch):
    monkeypatch.setattr(config, "FILTER_SOURCES", config.FILTER_SOURCES + [
        {"name": "mine", "type": "domains", "url": "http://x/", "category": "my-list"}])
    entry = next(c for c in categories.available() if c["name"] == "my-list")
    assert entry["group"] == "Other" and entry["sources"] == ["mine"]


def test_groups_are_in_display_order():
    names = [g for g, _ in categories.group(categories.available())]
    assert names[0] == "Security" and names.index("Adult") < names.index("Gambling and games")


# ---- saved selection ---------------------------------------------------------

def test_block_categories_file_overrides_default_and_empty_means_none(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CATEGORIES_FILE", tmp_path / "cats.conf")
    monkeypatch.setattr(config, "FILTER_BLOCK_CATEGORIES", ["adult"])
    assert config.block_categories() == ["adult"]  # no file: config.toml default
    (tmp_path / "cats.conf").write_text("# c\nGambling  # x\nbad name!\nadult\ngambling\n")
    assert config.block_categories() == ["gambling", "adult"]  # lower-cased, de-duplicated, invalid dropped
    (tmp_path / "cats.conf").write_text("# nothing\n")
    assert config.block_categories() == []


def test_render_and_parse_round_trip():
    text = categories.render_categories_file(["adult", "vpn"])
    assert config.parse_categories_text(text) == ["adult", "vpn"] and text.startswith("# ")


# ---- the admin page ----------------------------------------------------------

class _Lists(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        time.sleep(0.7)  # slow enough for a second save to arrive mid-download
        body = b"\n".join(f"d{i}{self.path.replace('/', '-')}.example".encode() for i in range(200))
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def admin(tmp_path):
    lists = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Lists)
    threading.Thread(target=lists.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{lists.server_address[1]}"
    (tmp_path / "config.toml").write_text(
        "[[filtering.sources]]\nname='src-a'\ntype='domains'\nurl='%s/adult'\ncategory='adult'\n"
        "[[filtering.sources]]\nname='src-g'\ntype='domains'\nurl='%s/gambling'\ncategory='gambling'\n" % (base, base))
    cats = tmp_path / "filter-categories.conf"
    cats.write_text("# managed\nadult\n")
    env = os.environ.copy()
    env.update({
        "CACHE_PROXY_CONFIG": str(tmp_path / "config.toml"),
        "CACHE_PROXY_CATEGORIES_FILE": str(cats),
        "CACHE_PROXY_LISTS_DIR": str(tmp_path / "lists"),
        "CACHE_PROXY_DIR": str(tmp_path / "files"),
        "CACHE_PROXY_DB": str(tmp_path / "index.db"),
        "CACHE_PROXY_WEBUI_USERNAME": "admin",
        "CACHE_PROXY_WEBUI_PASSWORD_HASH": hash_password("pw-12345"),
    })
    port = _free_port()
    proc = _serve("cache_proxy.webui.app:app", port, env)
    yield f"http://127.0.0.1:{port}", cats, tmp_path / "lists"
    proc.terminate()
    proc.wait(timeout=5)
    lists.shutdown()


def _post(base, pairs, headers=None):
    body = urllib.parse.urlencode(pairs)
    req = urllib.request.Request(base + "/categories", data=body.encode(), headers={**_auth(), **(headers or {})})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        r = urllib.request.build_opener(NoRedirect).open(req, timeout=15)
    except urllib.error.HTTPError as e:
        r = e
    return r.code, r.headers.get("Location", "")


def test_page_lists_categories_with_current_selection_ticked(admin):
    base, _, _ = admin
    code, body, _ = _req(base + "/categories", headers=_auth())
    assert code == 200
    assert 'value="adult"' in body and 'value="gambling"' in body
    assert 'id="cat-adult"' in body and "checked" in body.split('id="cat-adult"')[1].split(">")[0]
    assert "checked" not in body.split('id="cat-gambling"')[1].split(">")[0]
    assert "1 of 2 categories enforced" in body


def test_save_writes_the_file_and_downloads_newly_ticked_lists(admin):
    base, cats, lists = admin
    code, location = _post(base, [("cat", "gambling"), ("cat", "adult")])
    assert code == 303 and location.startswith("/categories?") and "ok=1" in location
    assert "Downloading" in urllib.parse.unquote_plus(location) and "adult" in location
    assert config.parse_categories_text(cats.read_text()) == ["adult", "gambling"]
    assert cats.read_text().startswith("# ")  # header kept
    # both were never downloaded, so both are fetched in the background
    assert _wait_for(lambda: (lists / "gambling" / "src-g.idx").exists() and (lists / "adult" / "src-a.idx").exists(), 20)
    body = _req(base + "/categories", headers=_auth())[1]
    assert "2 of 2 categories enforced" in body and ">200<" in body
    # already downloaded: saving again fetches nothing new
    code, location = _post(base, [("cat", "gambling")])
    assert "Downloading" not in urllib.parse.unquote_plus(location)
    assert config.parse_categories_text(cats.read_text()) == ["gambling"]


def test_saving_again_mid_download_does_not_queue_the_same_lists_twice(admin):
    base, cats, lists = admin
    _, first = _post(base, [("cat", "gambling"), ("cat", "adult")])
    assert "Downloading+lists+for" in first
    _, second = _post(base, [("cat", "gambling"), ("cat", "adult")])  # while the first is still running
    assert "Downloading+lists+for" not in second and "already+downloading" in second
    assert _wait_for(lambda: (lists / "gambling" / "src-g.idx").exists() and (lists / "adult" / "src-a.idx").exists(), 20)


def test_unknown_names_are_ignored_and_empty_selection_is_allowed(admin):
    base, cats, _ = admin
    _post(base, [("cat", "bogus"), ("cat", "../../etc/passwd")])
    assert config.parse_categories_text(cats.read_text()) == []
    assert "Saved: 0 categories" in urllib.parse.unquote_plus(_post(base, [])[1])


def test_cross_site_post_is_refused(admin):
    base, cats, _ = admin
    before = cats.read_text()
    code, _ = _post(base, [("cat", "gambling")], headers={"Origin": "http://evil.example"})
    assert code == 403 and cats.read_text() == before
    code, _ = _post(base, [("cat", "gambling")], headers={"Origin": base})  # same origin is fine
    assert code == 303


def test_unwritable_file_is_reported_not_crashed(admin):
    base, cats, _ = admin
    cats.chmod(0o444)
    if os.access(cats, os.W_OK):
        pytest.skip("running as a user that ignores file permissions")
    code, location = _post(base, [("cat", "gambling")])
    assert code == 303 and "not+writable" in location
    assert "not writable" in _req(base + "/categories", headers=_auth())[1]


def test_requires_login(admin):
    base, _, _ = admin
    assert _req(base + "/categories")[0] == 401
    assert _post.__name__  # (POST without credentials is covered by the global auth dependency)


def test_shared_header_menu_appears_on_every_admin_page_with_active_item(admin):
    base, _, _ = admin
    pages = {"/": "Cached Files", "/usage": "Usage", "/stats": "Stats", "/blocks": "Blocked", "/categories": "Categories"}
    for path, label in pages.items():
        body = _req(base + path, headers=_auth())[1]
        for href, name in (("/", "Cached Files"), ("/usage", "Usage"), ("/stats", "Stats"),
                           ("/blocks", "Blocked"), ("/categories", "Categories")):
            assert f'href="{href}"' in body and name in body, (path, name)
        assert f'class="active">{label}<' in body, path  # this page's item is highlighted
        assert body.count('class="active"') == 1
