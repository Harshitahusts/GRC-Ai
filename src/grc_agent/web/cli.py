"""`grc-web`: run the local web app and manage user accounts.

grc-web adduser NAME     create an account (prompts for a password)
grc-web passwd NAME      change an account's password
grc-web serve            start the app on http://127.0.0.1:8000
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

    for name, text in (("adduser", "Create an account."), ("passwd", "Change a password.")):
        p = sub.add_parser(name, help=text)
        p.add_argument("username")
        p.add_argument(
            "--password-stdin", action="store_true", help="Read the password from stdin."
        )

    args = parser.parse_args(argv)
    data_dir = Path(args.data_dir)

    if args.command == "serve":
        return _serve(data_dir, args.host, args.port, args.reload)
    return _set_password(
        data_dir, args.username, args.password_stdin, create=args.command == "adduser"
    )


def _serve(data_dir: Path, host: str, port: int, reload: bool) -> int:
    import uvicorn

    os.environ["GRC_DATA_DIR"] = str(data_dir)
    print(f"GRC agent running at http://{host}:{port}  (data: {data_dir.resolve()})")
    uvicorn.run(
        "grc_agent.web.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )
    return 0


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
