"""Keep vendor root/intermediate CAs trusted for upstream TLS verification.

Some vendors run their own CA hierarchy for parts of their infrastructure
rather than using the public web PKI roots already carried by
'ca-certificates' -- Windows Update is the first one found (Microsoft's
"ECC/RSA Update Secure Server CA" chains, rooted at "Microsoft ECC Product
Root Certificate Authority 2018" / "Microsoft Root Certificate Authority
2011", neither in Mozilla's included root program). mitmproxy verifies the
*upstream* (real server) certificate against certifi's bundle plus
ssl_verify_upstream_trusted_confdir; anything missing there fails with
"unable to get local issuer certificate" and mitmproxy serves a warning
instead of a stream -- which the client on that TCP flow experiences as a
hung connection, retried forever.

    python -m cache_proxy.vendorcas update
    python -m cache_proxy.vendorcas status

For each seed host in config.toml's [tls_trust] section: connect, then
walk the issuer chain via each certificate's Authority Information Access
"CA Issuers" URL until a self-signed root is reached (capped depth), and
write any certificate in that chain not already present to <dir> as PEM,
then re-hash the directory (openssl rehash) so mitmproxy's confdir lookup
finds it.

This discovers a vendor's whole chain from one TLS handshake against a
real endpoint of theirs -- nothing is pinned to a specific CA name,
fingerprint, or download URL, so a routine CA rotation upstream (a new
"... CA 2.2" replacing "2.1") is picked up on the next scheduled run with
no config change. Adding a new vendor later means adding a seed host, not
a certificate.
"""
import argparse
import json
import logging
import socket
import ssl
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from cache_proxy import config

logger = logging.getLogger("cache_proxy.vendorcas")

CONNECT_TIMEOUT = 10
FETCH_TIMEOUT = 30
STATUS_FILE = "status.json"


def _leaf_cert_der(host: str, port: int = 443) -> bytes:
    """The host's own leaf certificate, unverified -- we only need its
    Authority Information Access field to start the chain walk, not to
    trust the connection itself."""
    ctx = ssl._create_unverified_context()
    with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            return tls.getpeercert(binary_form=True)


def _fetch_cert_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "cache-proxy-vendorcas/1"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        return resp.read()


def _load_cert(data: bytes) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(data)
    except ValueError:
        return x509.load_der_x509_certificate(data)


def _aia_issuer_url(cert: x509.Certificate) -> Optional[str]:
    try:
        aia = cert.extensions.get_extension_for_class(x509.AuthorityInformationAccess).value
    except x509.ExtensionNotFound:
        return None
    for desc in aia:
        if desc.access_method == x509.AuthorityInformationAccessOID.CA_ISSUERS:
            return desc.access_location.value
    return None


def _is_self_signed(cert: x509.Certificate) -> bool:
    """Subject == issuer is necessary but not sufficient -- actually check
    the signature so a malformed or spoofed "root" fetched over plain
    HTTP (AIA URLs are almost always http://) is never trusted on name
    alone."""
    if cert.subject != cert.issuer:
        return False
    pub = cert.public_key()
    try:
        if isinstance(pub, rsa.RSAPublicKey):
            from cryptography.hazmat.primitives.asymmetric import padding
            pub.verify(cert.signature, cert.tbs_certificate_bytes, padding.PKCS1v15(), cert.signature_hash_algorithm)
        elif isinstance(pub, ec.EllipticCurvePublicKey):
            pub.verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(cert.signature_hash_algorithm))
        else:
            return False  # unsupported key type (e.g. Ed25519) -- don't trust what we can't check
        return True
    except Exception:
        return False


def build_chain(leaf_der: bytes, fetch: Callable[[str], bytes] = _fetch_cert_bytes,
                 max_depth: int = 6) -> list:
    """Certificates above the leaf, in order, from the AIA chain walk.
    The chain is only complete/trustworthy if the last entry passes
    _is_self_signed -- a truncated result (missing AIA, fetch failure, or
    max_depth reached first) ends with a non-root entry and callers must
    not add it to the trust store."""
    chain = []
    cert = x509.load_der_x509_certificate(leaf_der)
    for _ in range(max_depth):
        if _is_self_signed(cert):
            chain.append(cert)
            return chain
        url = _aia_issuer_url(cert)
        if not url:
            return chain
        cert = _load_cert(fetch(url))
        chain.append(cert)
        if _is_self_signed(cert):
            return chain
    return chain


def _fingerprint_name(cert: x509.Certificate) -> str:
    if cert.signature_hash_algorithm is not None:
        return cert.fingerprint(cert.signature_hash_algorithm).hex() + ".pem"
    return cert.public_bytes(serialization.Encoding.DER)[:16].hex() + ".pem"


def _write_cert(dir_: Path, cert: x509.Certificate) -> bool:
    """True if this was a new file (not already present)."""
    name = _fingerprint_name(cert)
    target = dir_ / name
    if target.exists():
        return False
    dir_.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    tmp.replace(target)
    return True


def _rehash(dir_: Path) -> None:
    subprocess.run(["openssl", "rehash", str(dir_)], check=True, capture_output=True, text=True)


def update_host(host: str, dir_: Path, get_leaf=None, fetch=None,
                 max_depth: int = 6) -> dict:
    # Resolved by name at call time (not as default-argument values bound
    # at def time) so tests can monkeypatch the module-level functions.
    get_leaf = get_leaf or _leaf_cert_der
    fetch = fetch or _fetch_cert_bytes
    result = {"host": host, "ts": time.time(), "added": None, "error": None}
    try:
        leaf_der = get_leaf(host)
        chain = build_chain(leaf_der, fetch, max_depth)
        if not chain or not _is_self_signed(chain[-1]):
            raise ValueError(f"no verified self-signed root within {max_depth} hops")
        result["added"] = sum(_write_cert(dir_, cert) for cert in chain)
    except Exception as e:  # one bad host must not stop the rest
        result["error"] = f"{type(e).__name__}: {e}"
    return result


def read_status() -> dict:
    try:
        return json.loads((config.VENDOR_CA_DIR / STATUS_FILE).read_text())
    except (OSError, ValueError):
        return {}


def _write_status(results: list) -> None:
    status = read_status()
    for r in results:
        prev = status.get(r["host"], {})
        if r["error"]:
            status[r["host"]] = {**prev, "last_error": r["error"], "last_error_ts": r["ts"]}
        else:
            status[r["host"]] = {"ts": r["ts"], "added": r["added"], "last_error": None}
    config.VENDOR_CA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.VENDOR_CA_DIR / (STATUS_FILE + ".tmp")
    tmp.write_text(json.dumps(status, indent=1))
    tmp.replace(config.VENDOR_CA_DIR / STATUS_FILE)


def update_all(hosts: Optional[list] = None) -> list:
    hosts = list(hosts) if hosts is not None else config.vendor_ca_seed_hosts()
    dir_ = config.VENDOR_CA_DIR
    results = [update_host(h, dir_, max_depth=config.VENDOR_CA_MAX_DEPTH) for h in hosts]
    for r in results:
        logger.info("%s: %s", r["host"], r["error"] or f"{r['added']} new CA cert(s)")
    if results:
        _write_status(results)
        if any(r["added"] for r in results if not r["error"]):
            _rehash(dir_)
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="cache_proxy.vendorcas")
    sub = ap.add_subparsers(dest="cmd", required=True)
    up = sub.add_parser("update", help="(re)discover and trust vendor root/intermediate CAs")
    up.add_argument("--host", action="append", dest="hosts", help="only this seed host (repeatable)")
    sub.add_parser("status", help="show what was last discovered")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.cmd == "status":
        for host, v in sorted(read_status().items()):
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(v["ts"])) if v.get("ts") else "never"
            print(f"{host:40} added {v.get('added', 0):>3}  updated {when}"
                  + (f"  LAST ERROR: {v['last_error']}" if v.get("last_error") else ""))
        return 0

    if not config.TLS_TRUST_ENABLED:
        print("tls_trust is disabled in config.toml", file=sys.stderr)
        return 0
    results = update_all(args.hosts)
    return 1 if any(r["error"] for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
