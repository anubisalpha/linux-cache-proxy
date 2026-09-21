"""Block-list downloader: parsing per source type, atomic refresh, and
never replacing a good list with a broken download."""
import io
import tarfile

import pytest

from cache_proxy import config, filterlists


def _tgz(files: dict) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def lists(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "FILTER_LISTS_DIR", tmp_path / "lists")
    monkeypatch.setattr(config, "CATEGORIES_FILE", tmp_path / "no-such-categories.conf")
    monkeypatch.setattr(config, "FILTER_BLOCK_CATEGORIES", ["adult", "malware", "phishing"])
    monkeypatch.setattr(config, "FILTER_SOURCES", [
        {"name": "ut1", "type": "ut1", "url": "http://x/{category}.tgz", "categories": ["adult", "gambling"]},
        {"name": "urlhaus", "type": "hosts", "url": "http://x/hosts", "category": "malware", "auth_key_env": "K"},
        {"name": "phish", "type": "domains", "url": "http://x/phish", "category": "phishing"},
    ])
    served = {}

    def fake_fetch(url, headers=None):
        served["last_headers"] = headers
        return served[url] if isinstance(served.get(url), bytes) else (_ for _ in ()).throw(OSError("boom"))

    monkeypatch.setattr(filterlists, "_fetch", fake_fetch)
    return served, tmp_path / "lists"


def test_update_writes_each_source_and_only_enforced_categories(lists):
    served, d = lists
    served["http://x/adult.tgz"] = _tgz({"adult/domains": "a.example\nb.example\n", "adult/urls": "ignored"})
    served["http://x/hosts"] = b"# hdr\n127.0.0.1\tm.example\n127.0.0.1\tn.example\n"
    served["http://x/phish"] = b"p1.example\np2.example\n"
    results = filterlists.update_all()
    assert not any(r["error"] for r in results)
    assert (d / "adult" / "ut1.txt").read_text().split() == ["a.example", "b.example"]
    assert (d / "malware" / "urlhaus.txt").read_text().split() == ["m.example", "n.example"]
    assert (d / "phishing" / "phish.txt").read_text().split() == ["p1.example", "p2.example"]
    assert not (d / "gambling").exists()  # not an enforced category: never downloaded
    # ...and the compact index the proxy actually loads is written alongside.
    from cache_proxy import contentfilter
    f = contentfilter.ContentFilter(tmp_path_of(d) / "b", tmp_path_of(d) / "a", tmp_path_of(d) / "p", d,
                                    categories=["adult", "malware", "phishing"])
    assert f.check("www.a.example", "http://www.a.example/").source == "ut1"
    assert f.check("n.example", "http://n.example/").category == "malware"
    assert f.check("p2.example", "http://p2.example/").source == "phish"
    assert f.check("other.example", "http://other.example/") is None


def tmp_path_of(lists_dir):
    return lists_dir.parent


def test_only_categories_limits_a_run(lists):
    served, d = lists
    served["http://x/adult.tgz"] = _tgz({"adult/domains": "a.example\n"})
    served["http://x/phish"] = b"p1.example\n"
    (r,) = filterlists.update_all(only_categories=["phishing"])
    assert r["category"] == "phishing" and not (d / "adult").exists()


def test_the_saved_selection_decides_what_is_downloaded(lists, tmp_path):
    served, d = lists
    (tmp_path / "no-such-categories.conf").write_text("adult\n")  # file present: it wins over the default
    served["http://x/adult.tgz"] = _tgz({"adult/domains": "a.example\n"})
    results = filterlists.update_all()
    assert [(r["source"], r["category"]) for r in results] == [("ut1", "adult")]


def test_hourly_run_fetches_only_sources_marked_hourly(lists, monkeypatch):
    served, d = lists
    monkeypatch.setattr(config, "FILTER_SOURCES", [
        {"name": "ut1", "type": "ut1", "url": "http://x/{category}.tgz", "categories": ["adult"]},
        {"name": "phish", "type": "domains", "url": "http://x/phish", "category": "phishing", "refresh": "hourly"},
    ])
    served["http://x/adult.tgz"] = _tgz({"adult/domains": "a.example\n"})
    served["http://x/phish"] = b"p1.example\n"
    results = filterlists.update_all(frequency="hourly")
    assert [r["source"] for r in results] == ["phish"]
    assert not (d / "adult").exists()
    assert {r["source"] for r in filterlists.update_all()} == {"ut1", "phish"}  # the daily run does all


def test_hourly_run_with_nothing_marked_hourly_is_not_a_failure(lists, monkeypatch):
    monkeypatch.setattr(config, "FILTER_SOURCES", [
        {"name": "ut1", "type": "ut1", "url": "http://x/{category}.tgz", "categories": ["adult"]}])
    assert filterlists.main(["update", "--frequency", "hourly"]) == 0
    assert filterlists.main(["update", "--source", "nonexistent"]) == 1


def test_ut1_tarball_with_the_wrong_category_is_an_error_not_mislabelled(lists):
    served, d = lists
    served["http://x/adult.tgz"] = _tgz({"phishing/domains": "p.example\n"})  # what UT1's malware tarball did
    (r,) = filterlists.update_all("ut1")
    assert "no adult/domains" in r["error"] and not (d / "adult").exists()


def test_split_ut1_domains_files_are_joined(lists):
    served, d = lists
    served["http://x/adult.tgz"] = _tgz({"adult/domains.0": "a.example\n", "adult/domains.1": "b.example\n"})
    filterlists.update_all("ut1")
    assert (d / "adult" / "ut1.txt").read_text().split() == ["a.example", "b.example"]


def test_failed_download_keeps_previous_list_and_is_reported(lists):
    served, d = lists
    served["http://x/phish"] = b"p1.example\np2.example\n"
    filterlists.update_all("phish")
    del served["http://x/phish"]
    (r,) = filterlists.update_all("phish")
    assert "OSError" in r["error"]
    assert (d / "phishing" / "phish.txt").read_text().split() == ["p1.example", "p2.example"]
    st = filterlists.read_status()["phish/phishing"]
    assert st["count"] == 2 and "OSError" in st["last_error"]


def test_truncated_or_empty_download_does_not_replace_a_good_list(lists):
    served, d = lists
    served["http://x/phish"] = b"\n".join(f"p{i}.example".encode() for i in range(100))
    filterlists.update_all("phish")
    served["http://x/phish"] = b"p1.example\n"  # 1 < 50% of 100
    (r,) = filterlists.update_all("phish")
    assert "keeping the previous list" in r["error"]
    assert len((d / "phishing" / "phish.txt").read_text().split()) == 100
    served["http://x/phish"] = b"# nothing\n"
    assert "no domains" in filterlists.update_all("phish")[0]["error"]


def test_auth_key_sent_from_environment(lists, monkeypatch):
    served, _ = lists
    monkeypatch.setenv("K", "sekret")
    served["http://x/hosts"] = b"127.0.0.1 m.example\n"
    filterlists.update_all("urlhaus")
    assert served["last_headers"] == {"Auth-Key": "sekret"}
    monkeypatch.delenv("K")
    filterlists.update_all("urlhaus")
    assert served["last_headers"] == {}
