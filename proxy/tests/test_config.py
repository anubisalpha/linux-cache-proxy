from cache_proxy import config


def test_load_host_list_parses_comments_and_blank_lines(tmp_path):
    f = tmp_path / "hosts.conf"
    f.write_text(
        "# a comment\n"
        "\n"
        "example.com\n"
        "  another.example.com  # trailing comment\n"
        "*.wildcard.example.com\n"
    )
    hosts = config._load_host_list(f)
    assert hosts == ["example.com", "another.example.com", "*.wildcard.example.com"]


def test_load_host_list_missing_file_returns_empty(tmp_path):
    assert config._load_host_list(tmp_path / "does-not-exist.conf") == []


def test_host_matches_bare_domain():
    assert config.host_matches("example.com", ["example.com"])


def test_host_matches_subdomain():
    assert config.host_matches("download.example.com", ["example.com"])


def test_host_matches_wildcard_prefix_form():
    assert config.host_matches("download.example.com", ["*.example.com"])
    assert config.host_matches("example.com", ["*.example.com"])


def test_host_matches_no_match():
    assert not config.host_matches("evil-example.com", ["example.com"])
    assert not config.host_matches("example.com.evil.com", ["example.com"])


def test_host_matches_empty_patterns():
    assert not config.host_matches("example.com", [])


def test_host_to_regex_matches_bare_and_subdomain():
    import re
    pattern = config.host_to_regex("example.com")
    assert re.search(pattern, "example.com")
    assert re.search(pattern, "download.example.com")
    assert not re.search(pattern, "evil-example.com")
    assert not re.search(pattern, "example.com.evil.com")


def test_host_to_regex_matches_host_with_port():
    """mitmproxy matches ignore_hosts against "host:port", not the bare
    hostname. Anchoring on the hostname alone means the pattern never
    matches a real request and never-intercept-hosts.conf is silently
    ignored for HTTPS -- the only traffic it exists for."""
    import re
    pattern = config.host_to_regex("example.com")
    assert re.search(pattern, "example.com:443")
    assert re.search(pattern, "download.example.com:443")
    assert re.search(pattern, "example.com:8443")
    # the port must not become a way past the anchor
    assert not re.search(pattern, "example.com.evil.com:443")
    assert not re.search(pattern, "evil-example.com:443")


def test_host_to_regex_strips_wildcard_prefix():
    import re
    pattern = config.host_to_regex("*.example.com")
    assert re.search(pattern, "example.com")
    assert re.search(pattern, "download.example.com")


# ---- vendor_ca_seed_hosts / add_vendor_ca_seed_host (web UI Certificates page) ---

def test_vendor_ca_seed_hosts_merges_toml_and_extra_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VENDOR_CA_SEED_HOSTS", ["fromtoml.example.com"])
    extra = tmp_path / "extra.conf"
    extra.write_text("fromui.example.com\n")
    monkeypatch.setattr(config, "VENDOR_CA_EXTRA_SEED_HOSTS_FILE", extra)
    assert config.vendor_ca_seed_hosts() == ["fromtoml.example.com", "fromui.example.com"]


def test_vendor_ca_seed_hosts_dedupes_across_both_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VENDOR_CA_SEED_HOSTS", ["dup.example.com"])
    extra = tmp_path / "extra.conf"
    extra.write_text("dup.example.com\nnew.example.com\n")
    monkeypatch.setattr(config, "VENDOR_CA_EXTRA_SEED_HOSTS_FILE", extra)
    assert config.vendor_ca_seed_hosts() == ["dup.example.com", "new.example.com"]


def test_vendor_ca_seed_hosts_no_extra_file_yet(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VENDOR_CA_SEED_HOSTS", ["fromtoml.example.com"])
    monkeypatch.setattr(config, "VENDOR_CA_EXTRA_SEED_HOSTS_FILE", tmp_path / "does-not-exist.conf")
    assert config.vendor_ca_seed_hosts() == ["fromtoml.example.com"]


def test_valid_hostname_accepts_real_hostnames():
    assert config.valid_hostname("example.com")
    assert config.valid_hostname("fe2cr.update.microsoft.com")
    assert config.valid_hostname("  Example.COM  ")  # whitespace/case tolerated


def test_valid_hostname_rejects_garbage():
    assert not config.valid_hostname("")
    assert not config.valid_hostname("not a hostname")
    assert not config.valid_hostname("no-dot")
    assert not config.valid_hostname("http://example.com")
    assert not config.valid_hostname("example.com; rm -rf /")


def test_add_vendor_ca_seed_host_persists_and_reports_new(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VENDOR_CA_SEED_HOSTS", [])
    extra = tmp_path / "extra.conf"
    monkeypatch.setattr(config, "VENDOR_CA_EXTRA_SEED_HOSTS_FILE", extra)
    assert config.add_vendor_ca_seed_host("New.Example.com") is True
    assert "new.example.com" in extra.read_text()
    assert config.vendor_ca_seed_hosts() == ["new.example.com"]


def test_add_vendor_ca_seed_host_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VENDOR_CA_SEED_HOSTS", [])
    extra = tmp_path / "extra.conf"
    monkeypatch.setattr(config, "VENDOR_CA_EXTRA_SEED_HOSTS_FILE", extra)
    assert config.add_vendor_ca_seed_host("example.com") is True
    assert config.add_vendor_ca_seed_host("example.com") is False
    assert config.vendor_ca_seed_hosts() == ["example.com"]  # not duplicated


def test_add_vendor_ca_seed_host_already_in_toml(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VENDOR_CA_SEED_HOSTS", ["example.com"])
    extra = tmp_path / "extra.conf"
    monkeypatch.setattr(config, "VENDOR_CA_EXTRA_SEED_HOSTS_FILE", extra)
    assert config.add_vendor_ca_seed_host("example.com") is False
    assert not extra.exists()  # nothing written -- already known from config.toml
