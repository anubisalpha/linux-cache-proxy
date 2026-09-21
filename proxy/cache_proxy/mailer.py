"""Outgoing mail: unblock requests to the administrator, and a test message.

Settings are in config.toml [email]; the SMTP password is read from the
CACHE_PROXY_SMTP_PASSWORD environment variable (set in
/etc/cache-proxy/secrets.env), never from config.toml.

    python -m cache_proxy.mailer --test      # send a test message and report
"""
import smtplib
import ssl
import sys
import time
from email.message import EmailMessage

from cache_proxy import config


def missing_settings() -> list:
    """Names of required [email] settings that are still empty."""
    need = {"smtp_host": config.SMTP_HOST, "from_address": config.EMAIL_FROM, "unblock_recipient": config.UNBLOCK_RECIPIENT}
    return [k for k, v in need.items() if not v]


def configured() -> bool:
    return not missing_settings()


def _smtp_send(msg: EmailMessage) -> None:
    if config.SMTP_SECURITY == "ssl":
        server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=20, context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20)
    with server:
        if config.SMTP_SECURITY == "starttls":
            server.starttls(context=ssl.create_default_context())
        if config.SMTP_USERNAME:
            server.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        server.send_message(msg)


def _oneline(text: str, limit: int = 200) -> str:
    return " ".join(str(text).split())[:limit]


def _fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(ts))


def admin_instructions(host: str) -> str:
    """What an admin does to approve a request -- shared by the email and the
    admin UI so the two can't disagree."""
    return (
        f"To allow this site, add this line to {config.ALLOWED_HOSTS_FILE}:\n"
        f"    {host}\n"
        "It covers the host and all its subdomains, overrides every block rule, and takes\n"
        "effect within about 10 seconds. No restart is needed. To decline, do nothing."
    )


def build_unblock_request(row, note: str) -> EmailMessage:
    """Built from the stored database row, never from browser-supplied fields
    (only the free-text note comes from the user)."""
    msg = EmailMessage()
    msg["Subject"] = f"Unblock request: {_oneline(row['host'], 100)} from {_oneline(row['client_ip'], 60)}"
    msg["From"] = config.EMAIL_FROM
    msg["To"] = config.UNBLOCK_RECIPIENT
    msg.set_content(
        "A user has asked for a blocked site to be unblocked.\n\n"
        f"Reason blocked : {row['reason']}\n"
        f"Category       : {row['category'] or '-'}\n"
        f"List source    : {row['source'] or '-'}\n"
        f"Site (host)    : {row['host']}\n"
        f"URL            : {row['url']}\n"
        f"Client IP      : {row['client_ip']}\n"
        f"Blocked at     : {_fmt_ts(row['ts'])}\n"
        f"Browser        : {row['user_agent'] or '-'}\n"
        f"Block ID       : {row['id']}\n\n"
        f"User's note:\n{note.strip() or '(none given)'}\n\n"
        f"{admin_instructions(row['host'])}\n"
    )
    return msg


def send_unblock_request(row, note: str) -> None:
    _smtp_send(build_unblock_request(row, note))


def send_test() -> None:
    msg = EmailMessage()
    msg["Subject"] = "cache-proxy test email"
    msg["From"] = config.EMAIL_FROM
    msg["To"] = config.UNBLOCK_RECIPIENT
    msg.set_content(
        "This is a test message from the cache proxy.\n"
        "If you can read it, unblock requests will reach this address.\n"
    )
    _smtp_send(msg)


def main(argv=None) -> int:
    if "--test" not in (argv if argv is not None else sys.argv[1:]):
        print(__doc__)
        return 2
    missing = missing_settings()
    if missing:
        print("Not configured -- set these in [email] in config.toml: " + ", ".join(missing), file=sys.stderr)
        return 1
    try:
        send_test()
    except Exception as e:
        print(f"Test email FAILED: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print(f"Test email sent to {config.UNBLOCK_RECIPIENT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
