"""Vendor root/intermediate CA discovery: AIA chain-walking, self-signed
root verification, and writing only complete/verified chains to disk."""
import datetime

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import AuthorityInformationAccessOID, NameOID

from cache_proxy import config, vendorcas


def _name(cn):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(subject_cn, issuer_cn, signing_key, subject_key, aia_url=None):
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(subject_cn))
        .issuer_name(_name(issuer_cn))
        .public_key(subject_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
    )
    if aia_url:
        builder = builder.add_extension(
            x509.AuthorityInformationAccess(
                [x509.AccessDescription(AuthorityInformationAccessOID.CA_ISSUERS,
                                         x509.UniformResourceIdentifier(aia_url))]
            ),
            critical=False,
        )
    return builder.sign(signing_key, hashes.SHA256())


@pytest.fixture
def pki():
    """leaf -> intermediate -> root, each with an AIA CA Issuers URL
    pointing at the next fetch, like a real chain. Returns the certs plus
    a fetch(url) fake that serves the intermediate/root DER bytes."""
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    inter_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    root = _cert("Test Root CA", "Test Root CA", root_key, root_key)
    inter = _cert("Test Intermediate CA", "Test Root CA", root_key, inter_key,
                   aia_url="http://example/root.crt")
    leaf = _cert("leaf.example", "Test Intermediate CA", inter_key, leaf_key,
                  aia_url="http://example/intermediate.crt")

    served = {
        "http://example/intermediate.crt": inter.public_bytes(serialization.Encoding.DER),
        "http://example/root.crt": root.public_bytes(serialization.Encoding.DER),
    }
    return {
        "leaf_der": leaf.public_bytes(serialization.Encoding.DER),
        "leaf": leaf, "inter": inter, "root": root,
        "fetch": lambda url: served[url],
    }


def test_build_chain_walks_aia_to_a_verified_root(pki):
    chain = vendorcas.build_chain(pki["leaf_der"], fetch=pki["fetch"])
    assert [c.subject.rfc4514_string() for c in chain] == [
        pki["inter"].subject.rfc4514_string(),
        pki["root"].subject.rfc4514_string(),
    ]
    assert vendorcas._is_self_signed(chain[-1])


def test_build_chain_stops_and_is_incomplete_without_a_root(pki):
    # Drop the AIA extension on the intermediate: no way to reach the root.
    inter_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    dead_end = _cert("Test Intermediate CA", "Test Root CA",
                      rsa.generate_private_key(public_exponent=65537, key_size=2048), inter_key)
    served = {"http://example/intermediate.crt": dead_end.public_bytes(serialization.Encoding.DER)}
    chain = vendorcas.build_chain(pki["leaf_der"], fetch=lambda u: served[u])
    assert len(chain) == 1
    assert not vendorcas._is_self_signed(chain[-1])


def test_a_same_subject_issuer_cert_with_a_bad_signature_is_not_trusted():
    # Subject == issuer alone isn't enough: the embedded public key must
    # actually verify the signature. Sign with one key but embed another
    # key's public half, so the signature cannot possibly verify.
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    unrelated_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    forged = _cert("Test Root CA", "Test Root CA", signing_key, unrelated_key)
    assert not vendorcas._is_self_signed(forged)


def test_update_host_writes_the_whole_chain_and_rehashes(pki, tmp_path, monkeypatch):
    rehashed = []
    monkeypatch.setattr(vendorcas, "_rehash", lambda d: rehashed.append(d))
    result = vendorcas.update_host(
        "leaf.example", tmp_path,
        get_leaf=lambda host: pki["leaf_der"],
        fetch=pki["fetch"],
    )
    assert result["error"] is None
    assert result["added"] == 2
    pems = sorted(p.name for p in tmp_path.glob("*.pem"))
    assert len(pems) == 2


def test_update_host_reports_an_incomplete_chain_as_an_error(pki, tmp_path):
    result = vendorcas.update_host(
        "leaf.example", tmp_path,
        get_leaf=lambda host: pki["leaf_der"],
        fetch=lambda url: (_ for _ in ()).throw(OSError("network down")),
    )
    assert result["error"] is not None
    assert not list(tmp_path.glob("*.pem"))


def test_update_all_keeps_going_when_one_host_fails(pki, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "VENDOR_CA_DIR", tmp_path)
    monkeypatch.setattr(vendorcas, "_rehash", lambda d: None)

    def get_leaf(host):
        if host == "bad.example":
            raise OSError("connection refused")
        return pki["leaf_der"]

    monkeypatch.setattr(vendorcas, "_leaf_cert_der", get_leaf)
    monkeypatch.setattr(vendorcas, "_fetch_cert_bytes", pki["fetch"])
    results = vendorcas.update_all(["good.example", "bad.example"])
    by_host = {r["host"]: r for r in results}
    assert by_host["good.example"]["error"] is None
    assert by_host["good.example"]["added"] == 2
    assert "connection refused" in by_host["bad.example"]["error"]

    status = vendorcas.read_status()
    assert status["good.example"]["added"] == 2
    assert "connection refused" in status["bad.example"]["last_error"]
