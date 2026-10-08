"""API keys, and an MCP server so other AI apps can read the workspace.

MCP (the Model Context Protocol) is how AI apps such as Claude Desktop, Claude Code or
Cursor plug into other tools. This endpoint offers them the GRC Analyst's read-only
tools (clients, findings, risks, data flows, evidence, readiness plans, the obligations
register and the text of the Act), so a consultant can ask about their DPDP work from
the AI app they already use.

- Read-only: no MCP tool changes anything. The analyst's create_task action is not offered.
- Each person makes their own API key on the API keys page. It is shown once; only a
  SHA-256 hash is stored. A key acts as its owner (and stops working when it is revoked
  or the account is removed). Every tool call is written to the audit log.
- The endpoint takes the key in an Authorization: Bearer header only, never the
  browser's session cookie, so a web page can't use it on someone's behalf.

Transport: MCP "Streamable HTTP" in its simplest form, one JSON-RPC request per POST
answered with one JSON response (no streaming needed for these quick tools).
"""

# No "from __future__ import annotations": FastAPI must resolve the dependency types.

import hashlib
import json
import secrets
import sqlite3
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from grc_agent import __version__
from grc_agent.tools import run_tool
from grc_agent.web import analyst, db
from grc_agent.web.https import is_local_host

KEY_PREFIX = "grcf_"
PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
INSTRUCTIONS = (
    "GRC Flow: a DPDP (India's Digital Personal Data Protection Act 2023 and DPDP Rules "
    "2025) compliance workspace. Start with list_engagements to find client ids. All tools "
    "read live workspace data and change nothing. Findings and documents for clients come "
    "from the app's assessment and human review, not from chat."
)


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def key_hash(key: str) -> str:
    # The key is 256 random bits, so a fast hash is enough (no password stretching needed).
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def user_for_key(conn: sqlite3.Connection, key: str) -> str | None:
    """The account a key belongs to, if the key is live and the account still exists."""
    if not key.startswith(KEY_PREFIX):
        return None
    row = conn.execute(
        "SELECT k.id, u.username FROM api_keys k JOIN users u "
        "ON LOWER(u.username) = LOWER(k.username) "
        "WHERE k.key_hash = ? AND k.revoked_at IS NULL",
        (key_hash(key),),
    ).fetchone()
    if row is None:
        return None
    conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (db.now(), row["id"]))
    return row["username"]


def _result(rid: Any, result: dict) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": rid, "result": result})


def _error(rid: Any, code: int, message: str, status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}},
        status_code=status,
    )


def _origin_ok(request: Request) -> bool:
    """Refuse browser requests from other sites (DNS-rebinding protection, per the spec)."""
    origin = request.headers.get("origin")
    if not origin:
        return True  # AI apps don't send one
    host = request.headers.get("host", "")
    from urllib.parse import urlsplit

    parts = urlsplit(origin)
    return parts.netloc == host or is_local_host(parts.hostname or "")


def register(app: FastAPI) -> None:
    from grc_agent.web.app import (
        Conn,
        User,
        flash,
        form_with_csrf,
        redirect,
        render,
        user_role,
    )

    # ---- the API keys page

    @app.get("/settings/api-keys")
    def api_keys_page(request: Request, user: User, conn: Conn):
        is_admin = user_role(request) == "admin"
        rows = conn.execute(
            # Safe: the SQL text holds only names from this code; values are ? parameters.
            "SELECT * FROM api_keys "  # nosec B608  # noqa: S608
            + ("" if is_admin else "WHERE LOWER(username) = LOWER(?) ")
            + "ORDER BY revoked_at IS NOT NULL, id DESC",
            () if is_admin else (user,),
        ).fetchall()
        shown = request.session.pop("new_api_key", None)
        return render(
            request,
            "api_keys.html",
            keys=rows,
            new_key=shown,
            is_admin=is_admin,
            mcp_url=str(request.url_for("mcp_endpoint")),
        )

    @app.post("/settings/api-keys")
    async def api_key_create(request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        name = str(form.get("name", "")).strip()[:60] or "AI app"
        key = new_key()
        conn.execute(
            "INSERT INTO api_keys (username, name, key_hash, hint, created_at) VALUES (?,?,?,?,?)",
            (user, name, key_hash(key), key[:9] + "…" + key[-4:], db.now()),
        )
        db.audit(conn, user, "api_key_created", None, {"name": name})
        # Shown once, on the next page load, then gone: only the hash is kept.
        request.session["new_api_key"] = {"name": name, "key": key}
        return redirect("/settings/api-keys")

    @app.post("/settings/api-keys/{kid}/revoke")
    async def api_key_revoke(kid: int, request: Request, user: User, conn: Conn):
        await form_with_csrf(request)
        row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (kid,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="No such key")
        if row["username"].lower() != user.lower() and user_role(request) != "admin":
            raise HTTPException(status_code=403, detail="That key belongs to someone else.")
        if row["revoked_at"] is None:
            conn.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ?", (db.now(), kid))
            db.audit(
                conn, user, "api_key_revoked", None, {"name": row["name"], "owner": row["username"]}
            )
        flash(request, f"Revoked “{row['name']}”. Apps using it can no longer connect.")
        return redirect("/settings/api-keys")

    # ---- the MCP endpoint

    @app.get("/mcp", name="mcp_endpoint")
    def mcp_get():
        # No server-to-client stream is offered; clients fall back to plain POSTs.
        return Response(status_code=405, headers={"Allow": "POST"})

    @app.post("/mcp")
    async def mcp_post(request: Request):
        from grc_agent.web.app import public_demo

        if public_demo():
            return _error(None, -32600, "MCP is switched off in the public demo.", 403)
        if not _origin_ok(request):
            return _error(None, -32600, "Cross-site requests are not allowed.", 403)
        auth = request.headers.get("authorization", "")
        key = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        with db.connect(request.app.state.db_path) as conn:
            user = user_for_key(conn, key) if key else None
        if user is None:
            return JSONResponse(
                {"error": "A valid GRC Flow API key is required (Authorization: Bearer ...)."},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="GRC Flow"'},
            )
        try:
            message = json.loads(await request.body())
        except ValueError:
            return _error(None, -32700, "Parse error: send one JSON-RPC message.", 400)
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, -32600, "Send one JSON-RPC 2.0 message per request.", 400)
        method, rid = message.get("method"), message.get("id")
        params = message.get("params") or {}
        if rid is None:  # a notification (e.g. notifications/initialized): nothing to answer
            return Response(status_code=202)

        if method == "initialize":
            asked = params.get("protocolVersion")
            return _result(
                rid,
                {
                    "protocolVersion": asked
                    if asked in PROTOCOL_VERSIONS
                    else PROTOCOL_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "grc-flow", "title": "GRC Flow", "version": __version__},
                    "instructions": INSTRUCTIONS,
                },
            )
        if method == "ping":
            return _result(rid, {})
        tools = {t.name: t for t in analyst.analyst_tools(request.app, user, can_act=False)}
        if method == "tools/list":
            return _result(
                rid,
                {
                    "tools": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "inputSchema": t.input_schema,
                            "annotations": {"readOnlyHint": True, "openWorldHint": False},
                        }
                        for t in tools.values()
                    ]
                },
            )
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name not in tools:
                return _error(rid, -32602, f"Unknown tool: {name}")
            if not isinstance(arguments, dict):
                return _error(rid, -32602, "arguments must be an object.")
            content, is_error = await run_in_threadpool(run_tool, tools, name, arguments)
            with db.connect(request.app.state.db_path) as conn:
                eid = arguments.get("engagement_id")
                db.audit(
                    conn,
                    user,
                    "mcp_tool_called",
                    eid if isinstance(eid, int) else None,
                    {"tool": name, "error": is_error},
                )
            return _result(
                rid, {"content": [{"type": "text", "text": content}], "isError": is_error}
            )
        return _error(rid, -32601, f"Method not found: {method}")
