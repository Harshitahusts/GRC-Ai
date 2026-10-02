"""`grc-web`: run the local web app and manage user accounts.

grc-web adduser NAME     create an account (prompts for a password)
grc-web passwd NAME      change an account's password
grc-web serve            start the app on http://127.0.0.1:8000 (--https for HTTPS)
grc-web demo             start a separate demo tenant with sample clients on port 8001
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from grc_agent.llm import PROVIDERS
from grc_agent.web import db
from grc_agent.web.security import hash_password

MIN_PASSWORD_LENGTH = 10


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="grc-web", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--data-dir",
        default=os.getenv("GRC_DATA_DIR", "var"),
        help="Where the database and secret key live (default: ./var, or GRC_DATA_DIR).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Start the web app.")
    serve.add_argument("--host", default="127.0.0.1", help="Default 127.0.0.1 (this machine only).")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true", help="Restart on code changes (dev).")
    serve.add_argument("--open", action="store_true", help="Open the app in a browser.")
    serve.add_argument(
        "--lan",
        action="store_true",
        help="Share on your local network (listens on all interfaces, prints the link).",
    )
    serve.add_argument(
        "--https",
        action="store_true",
        help="Serve over HTTPS with a self-signed certificate (or GRC_TLS_CERT/GRC_TLS_KEY).",
    )

    demo = sub.add_parser(
        "demo", help="Start a separate demo tenant with sample data (never your main data)."
    )
    demo.add_argument(
        "--dir", default="var-demo", help="Folder for the demo tenant (default: ./var-demo)."
    )
    demo.add_argument("--port", type=int, default=8001)
    demo.add_argument("--reset", action="store_true", help="Throw away and re-create the demo.")
    demo.add_argument("--seed-only", action="store_true", help="Create the data, don't serve.")
    demo.add_argument("--open", action="store_true", help="Open the demo in a browser.")
    demo.add_argument(
        "--lan",
        action="store_true",
        help="Share the demo on your local network, e.g. https://192.168.1.20:8001",
    )
    demo.add_argument(
        "--https",
        action="store_true",
        help="Serve over HTTPS with a self-signed certificate (or GRC_TLS_CERT/GRC_TLS_KEY).",
    )
    demo.add_argument(
        "--live-seconds",
        type=float,
        default=None,
        help="How often a colleague acts in the live demo (0 turns it off; default 40).",
    )

    sub.add_parser("init", help="Create the first account if none exist.")

    mig = sub.add_parser(
        "migrate-to-postgres",
        help="Copy this workspace's SQLite data into the database in GRC_DATABASE_URL.",
    )
    mig.add_argument(
        "--url",
        default=None,
        help="PostgreSQL URL (default: GRC_DATABASE_URL). The database must be empty.",
    )

    for name, text in (("adduser", "Create an account."), ("passwd", "Change a password.")):
        p = sub.add_parser(name, help=text)
        p.add_argument("username")
        p.add_argument(
            "--password-stdin", action="store_true", help="Read the password from stdin."
        )

    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)

    if args.command == "serve":
        host = "0.0.0.0" if args.lan else args.host
        return _serve(data_dir, host, args.port, args.reload, args.open, args.https)
    if args.command == "demo":
        return _demo(args)
    if args.command == "init":
        return _init(data_dir)
    if args.command == "migrate-to-postgres":
        return _migrate(data_dir, args.url)
    return _set_password(
        data_dir, args.username, args.password_stdin, create=args.command == "adduser"
    )


def _migrate(data_dir: Path, url: str | None) -> int:
    if url:
        os.environ["GRC_DATABASE_URL"] = url
    if not os.getenv("GRC_DATABASE_URL"):
        print(
            "error: set GRC_DATABASE_URL (or pass --url) to a PostgreSQL database.", file=sys.stderr
        )
        return 2
    target = db.database_target(data_dir)
    print(f"Copying {data_dir / 'grc.db'} to {db.label(target)} ...")
    copied = db.copy_to_postgres(data_dir / "grc.db", target)
    for table, n in copied.items():
        if n:
            print(f"  {table}: {n}")
    print(
        f"Done: {sum(copied.values())} rows. Keep GRC_DATABASE_URL in .env so the app uses "
        "PostgreSQL from now on. The SQLite file is left as it was; keep it as a backup."
    )
    return 0


def _serve(
    data_dir: Path,
    host: str,
    port: int,
    reload: bool,
    open_browser: bool,
    use_https: bool = False,
) -> int:
    import uvicorn

    from grc_agent.web import https

    os.environ["GRC_DATA_DIR"] = str(data_dir)
    local = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    shared = host in ("0.0.0.0", "::")
    ip = lan_ip() if shared else None
    data_dir.mkdir(parents=True, exist_ok=True)
    tls = https.tls_files(data_dir, use_https, [ip] if ip else [])
    scheme = "https" if tls else "http"
    if tls:
        # The app reads this when it starts: session cookies become Secure.
        os.environ["GRC_HTTPS"] = "1"
    url = f"{scheme}://{local}:{port}"
    if _port_in_use(local, port):
        # Usually an older copy of the app. Opening the browser would show that copy,
        # running old code, so stop here instead.
        print(
            f"Port {port} is already in use, probably by another copy of the app.", file=sys.stderr
        )
        print(
            "Close that window (or press Ctrl+C in it), then start the app again.\n"
            "Can't find it? Task Manager > Details: end python.exe and grc-web.exe.\n"
            f"Or use another port: --port {port + 1}",
            file=sys.stderr,
        )
        return 1
    print(f"GRC Flow running at {url}  (data: {db.label(db.database_target(data_dir))})")
    if tls:
        print(f"HTTPS certificate: {tls[0]}")
        print(f"  SHA-256 fingerprint: {https.fingerprint(tls[0])}")
        if not os.getenv("GRC_TLS_CERT"):
            print(
                "  It is self-signed, so browsers warn once: choose Advanced > Proceed. The\n"
                "  connection is still encrypted. Compare the fingerprint to be sure it's yours."
            )
    if shared:
        if ip:
            print(f"On your network: {scheme}://{ip}:{port}  (share this link with colleagues)")
        else:
            print("Couldn't find this computer's network address. Run ipconfig to look it up.")
        print(
            "Anyone on the same network can open it. If Windows asks, allow Python through the\n"
            "firewall on Private networks only. Other devices must use the same Wi-Fi or LAN."
        )
        if not tls:
            print(
                "Warning: this is plain HTTP, so passwords cross the network unencrypted.\n"
                "Add --https to encrypt it.",
                file=sys.stderr,
            )
    print("Press Ctrl+C to stop.")
    if open_browser:
        import threading
        import webbrowser

        threading.Timer(1.5, webbrowser.open, args=(url,)).start()
    uvicorn.run(
        "grc_agent.web.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        log_level="info",
        ssl_certfile=tls[0] if tls else None,
        ssl_keyfile=tls[1] if tls else None,
        # Behind a reverse proxy that terminates HTTPS, trust its X-Forwarded-Proto
        # header only from the addresses listed here.
        forwarded_allow_ips=os.getenv("GRC_TRUSTED_PROXIES", "127.0.0.1"),
    )
    return 0


def _demo(args) -> int:
    from dotenv import load_dotenv

    from grc_agent.web import demo_tenant

    load_dotenv()
    if os.getenv("GRC_DATABASE_URL"):
        # On PostgreSQL the demo gets its own schema, so its sample data never mixes
        # with the real workspace in the same database.
        os.environ["GRC_DATABASE_SCHEMA"] = os.getenv("GRC_DEMO_SCHEMA", "grc_demo")
    if not any(os.getenv(p.key_env) for p in PROVIDERS.values() if p.key_env) and not os.getenv(
        "GRC_AI_PROVIDER"
    ):
        # No AI key at all: the analyst gives simulated answers instead of failing
        # mid-demo. A provider picked on the AI provider page still takes over.
        os.environ.setdefault("GRC_AI_MODE", "demo")
    if args.live_seconds is not None:
        os.environ["GRC_DEMO_LIVE_SECONDS"] = str(args.live_seconds)
    data_dir = Path(args.dir)
    fresh = args.reset or not demo_tenant.is_demo(data_dir)
    if fresh:
        print(f"Creating the demo tenant in {data_dir.resolve()} ...")
    demo_tenant.seed(data_dir, reset=args.reset)
    print(
        f"Demo tenant ready. Sign in as {demo_tenant.DEMO_USER} / {demo_tenant.DEMO_PASSWORD}"
        " (sample data, separate from your main workspace)."
    )
    if args.seed_only:
        return 0
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    return _serve(data_dir, host, args.port, False, args.open, args.https)


def lan_ip() -> str | None:
    """This computer's address on the local network. Sends nothing: a UDP connect
    only asks the OS which interface it would route through."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.254.254.254", 1))
            ip = s.getsockname()[0]
        except OSError:
            return None
    return None if ip.startswith("127.") else ip


def _port_in_use(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _init(data_dir: Path) -> int:
    db_path = db.database_target(data_dir)
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if count:
        print(f"{count} account(s) already exist. Skipping.")
        return 0
    print("No accounts yet. Create the first one.")
    username = input("Username: ").strip()
    return _set_password(data_dir, username, from_stdin=False, create=True)


def _set_password(data_dir: Path, username: str, from_stdin: bool, create: bool) -> int:
    username = username.strip()
    if not username or len(username) > 64:
        print("error: username must be 1-64 characters", file=sys.stderr)
        return 2
    if from_stdin:
        password = sys.stdin.readline().rstrip("\n")
    else:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat password: "):
            print("error: passwords don't match", file=sys.stderr)
            return 2
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"error: password must be at least {MIN_PASSWORD_LENGTH} characters", file=sys.stderr)
        return 2

    db_path = db.database_target(data_dir)
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        exists = conn.execute(
            "SELECT 1 FROM users WHERE LOWER(username) = LOWER(?)", (username,)
        ).fetchone()
        if create and exists:
            print(f"error: user {username!r} already exists (use passwd)", file=sys.stderr)
            return 1
        if not create and not exists:
            print(f"error: no user {username!r} (use adduser)", file=sys.stderr)
            return 1
        if create:
            conn.execute(
                "INSERT INTO users (username, password_hash, created_at) VALUES (?,?,?)",
                (username, hash_password(password), db.now()),
            )
        else:
            conn.execute(
                "UPDATE users SET password_hash = ? WHERE LOWER(username) = LOWER(?)",
                (hash_password(password), username),
            )
        db.audit(conn, "cli", "user_created" if create else "password_changed", detail=username)
    print(f"{'Created' if create else 'Updated'} user {username!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
