"""The AI provider page: pick Claude or a free OpenAI-compatible provider, test it.

Where the settings come from, highest priority first:
1. A provider an admin saved and switched on here (its key is encrypted in the database).
2. .env: GRC_AI_PROVIDER, or the first provider whose API key is set.
3. GRC_AI_MODE=demo, the offline stand-in.

A saved key never leaves the server: the page shows only a masked hint.
"""

import os
import sqlite3
from dataclasses import replace
from typing import Any

import anthropic
from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from grc_agent.config import Settings, make_client
from grc_agent.connectors.secrets import mask
from grc_agent.llm import PROVIDERS, OpenAICompatClient, key_status
from grc_agent.web import db

TEST_PROMPT = "Reply with the single word OK."


def saved(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT * FROM ai_providers").fetchall()
    return {r["provider"]: dict(r) for r in rows}


def resolve(app: FastAPI, conn: sqlite3.Connection | None = None) -> tuple[Settings, Any | None]:
    """The settings in force and, for a provider saved on the page, a ready client.

    A None client means "build it from .env as usual" (make_client).
    """
    base = Settings.from_env()
    if conn is None:
        with db.connect(app.state.db_path) as c:
            return resolve(app, c)
    row = conn.execute("SELECT * FROM ai_providers WHERE active = 1").fetchone()
    if row is None or row["provider"] not in PROVIDERS:
        return base, None
    provider = PROVIDERS[row["provider"]]
    settings = replace(
        base,
        provider=provider.key,
        model=row["model"] or provider.model or base.model,
        ai_mode="api",
    )
    if provider.key != "anthropic" and not os.getenv("GRC_AGENT_MAX_TOKENS"):
        settings = replace(settings, max_tokens=min(settings.max_tokens, 4096))
    key = app.state.secret_box.open(row["key_enc"])["key"] if row["key_enc"] else None
    if provider.key == "anthropic":
        client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
    else:
        client = OpenAICompatClient(provider, api_key=key, base_url=row["base_url"] or None)
    return settings, client


def refresh(app: FastAPI) -> None:
    """Re-read the AI settings after a change; analysts restart with the new provider."""
    settings, client = resolve(app)
    app.state.ai = settings
    app.state.ai_client = client
    app.state.demo = settings.demo
    app.state.agents.clear()


def client_for(app: FastAPI) -> tuple[Settings, Any]:
    settings = app.state.ai
    return settings, app.state.ai_client or make_client(settings)


def register(app: FastAPI) -> None:
    from grc_agent.web.app import Conn, User, flash, form_with_csrf, redirect, render, user_role

    def require_admin(request: Request) -> None:
        if user_role(request) != "admin":
            raise HTTPException(status_code=403, detail="Only an admin can change the AI provider.")

    @app.get("/settings/ai")
    def ai_page(request: Request, user: User, conn: Conn):
        rows = saved(conn)
        env = {s["provider"].key: s["configured"] for s in key_status()}
        settings = request.app.state.ai
        active = next((k for k, r in rows.items() if r["active"]), None)
        return render(
            request,
            "ai_settings.html",
            providers=list(PROVIDERS.values()),
            rows=rows,
            env=env,
            settings=settings,
            source="page" if active else ("demo" if settings.demo else ".env"),
            is_admin=user_role(request) == "admin",
        )

    @app.post("/settings/ai")
    async def ai_save(request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        require_admin(request)
        key = str(form.get("provider", ""))
        if key not in PROVIDERS:
            raise HTTPException(status_code=400, detail="Unknown provider")
        provider = PROVIDERS[key]
        api_key = str(form.get("api_key", "")).strip()
        model = str(form.get("model", "")).strip()[:120]
        base_url = str(form.get("base_url", "")).strip()[:300]
        if base_url and not base_url.startswith(("http://", "https://")):
            flash(request, "The address must start with http:// or https://.", "error")
            return redirect("/settings/ai")
        if key == "custom" and not base_url:
            flash(request, "Give the address of your OpenAI-compatible server.", "error")
            return redirect("/settings/ai")
        old = saved(conn).get(key)
        if api_key:
            key_enc = request.app.state.secret_box.seal({"key": api_key})
            hint = mask(api_key)
        else:
            key_enc = old["key_enc"] if old else ""
            hint = old["key_hint"] if old else ""
        if provider.key_env and not key_enc and not key_status_of(key):
            flash(request, f"Paste your {provider.label} API key.", "error")
            return redirect("/settings/ai")
        conn.execute("UPDATE ai_providers SET active = 0")
        conn.execute("DELETE FROM ai_providers WHERE provider = ?", (key,))
        conn.execute(
            "INSERT INTO ai_providers (provider, model, base_url, key_enc, key_hint, active, "
            "updated_by, updated_at) VALUES (?,?,?,?,?,1,?,?)",
            (key, model, base_url, key_enc, hint, user, db.now()),
        )
        db.audit(conn, user, "ai_provider_set", None, {"provider": key, "model": model})
        conn.commit()
        refresh(request.app)
        flash(request, f"The GRC Analyst now uses {provider.label}. Test it below.")
        return redirect("/settings/ai")

    @app.post("/settings/ai/test")
    async def ai_test(request: Request, user: User, conn: Conn):
        await form_with_csrf(request)
        require_admin(request)
        settings, client = client_for(request.app)
        if settings.demo:
            flash(request, "Demo mode is on: there is no real AI to test.", "error")
            return redirect("/settings/ai")
        ok, message = await run_in_threadpool(probe, client, settings)
        conn.execute(
            "UPDATE ai_providers SET status = ?, message = ?, tested_at = ? WHERE active = 1",
            ("ok" if ok else "error", message, db.now()),
        )
        flash(request, message, None if ok else "error")
        return redirect("/settings/ai")

    @app.post("/settings/ai/off")
    async def ai_off(request: Request, user: User, conn: Conn):
        await form_with_csrf(request)
        require_admin(request)
        conn.execute("UPDATE ai_providers SET active = 0")
        db.audit(conn, user, "ai_provider_cleared", None, {})
        conn.commit()
        refresh(request.app)
        flash(request, "Using the settings in .env again.")
        return redirect("/settings/ai")

    @app.post("/settings/ai/{key}/forget")
    async def ai_forget(key: str, request: Request, user: User, conn: Conn):
        await form_with_csrf(request)
        require_admin(request)
        conn.execute("DELETE FROM ai_providers WHERE provider = ?", (key,))
        db.audit(conn, user, "ai_provider_removed", None, {"provider": key})
        conn.commit()
        refresh(request.app)
        flash(request, "Removed the saved key.")
        return redirect("/settings/ai")


def key_status_of(provider_key: str) -> bool:
    return next(s["configured"] for s in key_status() if s["provider"].key == provider_key)


def probe(client: Any, settings: Settings) -> tuple[bool, str]:
    """One tiny request, to prove the key, the address and the model all work."""
    label = settings.provider_label
    try:
        response = client.beta.messages.create(
            model=settings.model,
            max_tokens=200,
            messages=[{"role": "user", "content": TEST_PROMPT}],
        )
    except anthropic.AuthenticationError:
        return False, f"{label} rejected the API key. Check it and save again."
    except anthropic.NotFoundError:
        return False, f"{label} doesn't know the model {settings.model!r}. Pick another."
    except anthropic.RateLimitError:
        return False, f"{label} says you've hit the free-tier limit. Wait a minute and retry."
    except anthropic.APIConnectionError:
        return False, f"Could not reach {label}. Is the address right and the server running?"
    except anthropic.CredentialsError as exc:
        return False, str(exc)
    except anthropic.APIError as exc:
        return False, f"{label} error: {str(exc)[:200]}"
    text = " ".join(b.text for b in response.content if b.type == "text").strip()
    return True, f"Connected to {label} ({settings.model}). It replied: {text[:60] or '(empty)'}"
