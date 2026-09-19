"""Generate a password_hash line for [webui] in /etc/cache-proxy/config.toml.

Usage: python3 -m cache_proxy.webui.hash_password
"""
import getpass

from .auth import hash_password


def main() -> None:
    pw1 = getpass.getpass("New web UI password: ")
    pw2 = getpass.getpass("Confirm: ")
    if pw1 != pw2:
        print("Passwords did not match.")
        raise SystemExit(1)
    if not pw1:
        print("Password cannot be empty.")
        raise SystemExit(1)
    print()
    print("Add this to /etc/cache-proxy/config.toml under [webui]:")
    print()
    print(f'password_hash = "{hash_password(pw1)}"')


if __name__ == "__main__":
    main()
