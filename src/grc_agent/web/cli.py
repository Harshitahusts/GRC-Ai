"""`grc-web`: run the local web app and manage user accounts.

grc-web adduser NAME     create an account (prompts for a password)
grc-web passwd NAME      change an account's password
grc-web serve            start the app on http://127.0.0.1:8000
grc-web demo             start a separate demo tenant with sample clients on port 8001
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

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
        "--live-seconds",
        type=float,
        default=None,
        help="How often a colleague acts in the live demo (0 turns it off; default 40).",
    )

    sub.add_parser("init", help="Create the first account if none exist.")

    for name, text in (("adduser", "Create an account."), ("passwd", "Change a password.")):
        p = sub.add_parser(name, help=text)
        p.add_argument("username")
        p.add_argument(
            "--password-stdin", action="store_true", help="Read the password from stdin."
        )

    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)

    if args.command == "serve":
        return _serve(data_dir, args.host, args.port, args.reload, args.open)
    if args.command == "demo":
        return _demo(args)
    if args.command == "init":
        return _init(data_dir)
    return _set_password(
        data_dir, args.username, args.password_stdin, create=args.command == "adduser"
    )


def _serve(data_dir: Path, host: str, port: int, reload: bool, open_browser: bool) -> int:
    import uvicorn

    os.environ["GRC_DATA_DIR"] = str(data_dir)
    local = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{local}:{port}"
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
    print(f"GRC agent running at {url}  (data: {data_dir.resolve()})")
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
    )
    return 0


def _demo(args) -> int:
    from dotenv import load_dotenv

    from grc_agent.web import demo_tenant

    load_dotenv()
    if not os.getenv("ANTHROPIC_API_KEY"):
        # No key: the analyst gives simulated answers instead of failing mid-demo.
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
    return _serve(data_dir, "127.0.0.1", args.port, False, args.open)


def _port_in_use(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _init(data_dir: Path) -> int:
    db_path = data_dir / "grc.db"
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

    db_path = data_dir / "grc.db"
    db.init_db(db_path)
    with db.connect(db_path) as conn:
        exists = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
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
                "UPDATE users SET password_hash = ? WHERE username = ?",
                (hash_password(password), username),
            )
        db.audit(conn, "cli", "user_created" if create else "password_changed", detail=username)
    print(f"{'Created' if create else 'Updated'} user {username!r}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
