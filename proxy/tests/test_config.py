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
