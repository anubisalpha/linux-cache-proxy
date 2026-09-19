from cache_proxy.webui.auth import hash_password, verify_password


def test_hash_and_verify_roundtrip():
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h)


def test_wrong_password_rejected():
    h = hash_password("correct horse battery staple")
    assert not verify_password("wrong password", h)


def test_hash_is_salted_differently_each_time():
    h1 = hash_password("same password")
    h2 = hash_password("same password")
    assert h1 != h2
    assert verify_password("same password", h1)
    assert verify_password("same password", h2)


def test_verify_rejects_malformed_hash():
    assert not verify_password("anything", "not-a-real-hash")
    assert not verify_password("anything", "")
    assert not verify_password("anything", "pbkdf2_sha256$notanumber$abcd$abcd")


def test_verify_rejects_unknown_algorithm():
    assert not verify_password("anything", "md5$1000$abcd$abcd")
