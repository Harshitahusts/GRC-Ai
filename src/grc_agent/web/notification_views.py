"""Notification pages: the list, the bell's live count, and marking read."""

# No "from __future__ import annotations": FastAPI must resolve the dependency types.

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from grc_agent.web import notify


def _local(path: str) -> bool:
    """Only same-site paths: "/x" yes, "//evil.com" or "https://..." no."""
    return path.startswith("/") and not path.startswith("//") and "\\" not in path


def register(app: FastAPI) -> None:
    from grc_agent.web.app import Conn, User, form_with_csrf, redirect, render

    @app.get("/notifications")
    def notifications_page(
        request: Request,
        user: User,
        conn: Conn,
        show: str = "all",
        category: str = "",
        level: str = "",
    ):
        category = category if category in notify.CATEGORIES else ""
        level = level if level in notify.LEVELS else ""
        items = notify.listing(
            conn, user, unread_only=show == "unread", category=category, level=level
        )
        counts = {
            row["category"]: row["n"]
            for row in conn.execute(
                "SELECT category, COUNT(*) AS n FROM notifications GROUP BY category"
            )
        }
        return render(
            request,
            "notifications.html",
            items=items,
            show=show,
            category=category,
            level=level,
            categories=notify.CATEGORIES,
            counts=counts,
        )

    @app.get("/notifications/unread.json")
    def notifications_unread(user: User, conn: Conn, after: int = 0):
        """Polled by every page: the unread count, and anything new since `after`."""
        fresh = (
            notify.listing(conn, user, unread_only=True, after_id=after, limit=5) if after else []
        )
        latest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM notifications").fetchone()[0]
        return JSONResponse(
            {
                "unread": notify.unread_count(conn, user),
                "latest": latest,
                "new": [
                    {k: n[k] for k in ("id", "level", "title", "link")} for n in reversed(fresh)
                ],
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/notifications/read")
    async def notifications_read(request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        ids = [int(i) for i in str(form.get("ids", "")).split(",") if i.strip().isdigit()]
        notify.mark_read(conn, user, ids or None)
        back = str(form.get("back") or "")
        return redirect(back if _local(back) else "/notifications")

    @app.get("/notifications/{nid}/open")
    def notification_open(nid: int, user: User, conn: Conn):
        row = conn.execute("SELECT link FROM notifications WHERE id = ?", (nid,)).fetchone()
        notify.mark_read(conn, user, [nid])
        link = row["link"] if row and _local(row["link"]) else "/notifications"
        return redirect(link)
