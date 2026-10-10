"""A client's public DPDP trust page: what it does to protect personal data, in one link
it can put on its website, share with customers or attach to tenders.

Everything on it comes from records already kept in GRC Flow, and only the safe summary
of them: published policies, the safeguards in place across its systems, its active
sub-processors and where they process data, how to exercise rights, and which areas of
its DPDP programme are in place. Nothing internal (notes, owners, evidence, personal data)
is ever shown. It stays private until an admin or manager publishes it.
"""

# No "from __future__ import annotations": FastAPI must resolve the dependency types.

import json
import re
import sqlite3

from fastapi import FastAPI, HTTPException, Request

from grc_agent.web import access, db

SLUG = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,58}[a-z0-9])$")
RESERVED = {"admin", "api", "login", "static", "dashboard", "settings", "trust"}

# Programme areas shown publicly, each made of catalogue duties. An area reads "In place"
# only when every duty in it that applies is marked compliant.
AREAS = (
    ("Notice and consent", ("A-05", "A-06", "A-06b", "A-06c")),
    ("Security safeguards", ("A-08-5", "R-08-logs")),
    ("Breach response", ("A-08-6",)),
    ("Rights of Data Principals", ("A-11", "A-12", "A-13", "A-14", "A-08-10")),
    ("Retention and erasure", ("A-08-7", "R-08-48h")),
    ("Vendors and processors", ("A-08-2",)),
    ("Children's data", ("A-09-1", "A-09-2")),
    ("Transfers outside India", ("A-16",)),
)
SAFEGUARDS = (
    ("encryption", "Personal data encrypted or masked"),
    ("access_control", "Access limited to people who need it, and reviewed"),
    ("logging", "Access logged, logs kept for a year"),
    ("backups", "Backed up, with restores tested"),
)
LOCATIONS = {"india": "India", "outside": "Outside India", "both": "India and abroad", "": "–"}


def settings_of(conn: sqlite3.Connection, eid: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM trust_pages WHERE engagement_id = ?", (eid,)).fetchone()


def suggest_slug(conn: sqlite3.Connection, name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50] or "company"
    if len(base) < 3:
        base = f"{base}-dpdp"
    slug, n = base, 2
    while conn.execute("SELECT 1 FROM trust_pages WHERE slug = ?", (slug,)).fetchone():
        slug, n = f"{base}-{n}", n + 1
    return slug


def content(conn: sqlite3.Connection, eid: int, page: sqlite3.Row) -> dict:
    """Only what is safe to publish."""
    from grc_agent.web.obligation_views import items_for

    out: dict = {"areas": [], "safeguards": [], "policies": [], "subprocessors": []}
    if page["show_programme"]:
        status = {i["item"].id: i["status"] for i in items_for(conn, eid)}
        for name, ids in AREAS:
            applies = [status[i] for i in ids if status.get(i) != "not_applicable"]
            if not applies:
                continue
            state = (
                "in_place"
                if all(s == "compliant" for s in applies)
                else "progress"
                if any(s in ("compliant", "partial") for s in applies)
                else "planned"
            )
            out["areas"].append({"name": name, "state": state})
    records = conn.execute(
        "SELECT register, title, status, data_json, updated_at FROM records "
        "WHERE engagement_id = ? AND register IN ('systems', 'policies', 'vendors')",
        (eid,),
    ).fetchall()
    systems = [
        json.loads(r["data_json"])
        for r in records
        if r["register"] == "systems" and r["status"] != "retired"
    ]
    if page["show_safeguards"] and systems:
        for key, label in SAFEGUARDS:
            yes = sum(s.get(key) == "yes" for s in systems)
            out["safeguards"].append(
                {
                    "label": label,
                    "state": "in_place"
                    if yes == len(systems)
                    else "progress"
                    if yes
                    else "planned",
                }
            )
    if page["show_policies"]:
        for r in records:
            if r["register"] == "policies" and r["status"] == "published":
                d = json.loads(r["data_json"])
                link = d.get("link", "")
                out["policies"].append(
                    {
                        "title": r["title"],
                        "version": d.get("version", ""),
                        "approved_on": d.get("approved_on", ""),
                        "link": link if link.startswith("https://") else "",
                    }
                )
    if page["show_subprocessors"]:
        for r in records:
            if r["register"] == "vendors" and r["status"] == "active":
                d = json.loads(r["data_json"])
                out["subprocessors"].append(
                    {
                        "name": r["title"],
                        "service": d.get("service", ""),
                        "location": LOCATIONS.get(d.get("location", ""), "–"),
                    }
                )
        out["subprocessors"].sort(key=lambda v: v["name"].lower())
    dates = [page["updated_at"], *(r["updated_at"] for r in records)]
    out["updated"] = max(d for d in dates if d)[:10]
    return out


def register(app: FastAPI) -> None:
    from grc_agent.web.app import (
        Conn,
        User,
        _summary,
        flash,
        form_with_csrf,
        get_engagement,
        redirect,
        render,
    )
    from grc_agent.web.auth_views import app_url

    @app.get("/engagements/{eid}/trust")
    def trust_settings(eid: int, request: Request, user: User, conn: Conn):
        eng = get_engagement(conn, eid)
        page = settings_of(conn, eid)
        preview = content(conn, eid, page) if page else None
        return render(
            request,
            "trust_settings.html",
            eng=eng,
            s=_summary(conn, eng),
            tab="trust",
            page=page,
            preview=preview,
            slug=page["slug"] if page else suggest_slug(conn, eng["client"]),
            public_url=app_url(request) + "/trust/",
            can_edit=access.leads(conn, user, eid),
        )

    @app.post("/engagements/{eid}/trust")
    async def trust_save(eid: int, request: Request, user: User, conn: Conn):
        form = await form_with_csrf(request)
        eng = get_engagement(conn, eid)
        if not access.leads(conn, user, eid):
            raise HTTPException(status_code=403, detail="Only an admin or manager can do this.")
        back = f"/engagements/{eid}/trust"
        slug = str(form.get("slug", "")).strip().lower()
        if not SLUG.match(slug) or slug in RESERVED:
            flash(request, "Web address: 3-60 lowercase letters, numbers and dashes.", "error")
            return redirect(back)
        taken = conn.execute(
            "SELECT 1 FROM trust_pages WHERE slug = ? AND engagement_id != ?", (slug, eid)
        ).fetchone()
        if taken:
            flash(request, "That web address is taken. Choose another.", "error")
            return redirect(back)
        email = str(form.get("contact_email", "")).strip()[:200]
        rights_url = str(form.get("rights_url", "")).strip()[:300]
        if rights_url and not rights_url.startswith("https://"):
            flash(request, "The rights page link must start with https://", "error")
            return redirect(back)
        published = 1 if form.get("published") else 0
        if published and not email:
            flash(
                request,
                "Add a contact email before publishing: the Act requires one (Section 8(9)).",
                "error",
            )
            return redirect(back)
        values = (
            slug,
            published,
            str(form.get("company", "")).strip()[:160] or eng["client"],
            str(form.get("intro", "")).strip()[:1500],
            email,
            str(form.get("grievance_officer", "")).strip()[:160],
            rights_url,
            *(
                1 if form.get(k) else 0
                for k in (
                    "show_policies",
                    "show_safeguards",
                    "show_subprocessors",
                    "show_programme",
                )
            ),
            user,
            db.now(),
        )
        conn.execute(
            "INSERT INTO trust_pages (slug, published, company, intro, contact_email, "
            "grievance_officer, rights_url, show_policies, show_safeguards, "
            "show_subprocessors, show_programme, updated_by, updated_at, engagement_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (engagement_id) DO UPDATE SET "
            "slug = excluded.slug, published = excluded.published, company = excluded.company, "
            "intro = excluded.intro, contact_email = excluded.contact_email, "
            "grievance_officer = excluded.grievance_officer, rights_url = excluded.rights_url, "
            "show_policies = excluded.show_policies, show_safeguards = excluded.show_safeguards, "
            "show_subprocessors = excluded.show_subprocessors, "
            "show_programme = excluded.show_programme, updated_by = excluded.updated_by, "
            "updated_at = excluded.updated_at",
            (*values, eid),
        )
        db.audit(conn, user, "trust_page_saved", eid, {"slug": slug, "published": published})
        flash(request, "Trust page published." if published else "Trust page saved (not public).")
        return redirect(back)

    @app.get("/engagements/{eid}/trust/preview")
    def trust_preview(eid: int, request: Request, user: User, conn: Conn):
        get_engagement(conn, eid)
        page = settings_of(conn, eid)
        if page is None:
            return redirect(f"/engagements/{eid}/trust")
        return render(
            request,
            "trust_public.html",
            page=page,
            c=content(conn, eid, page),
            rights_days=90,
            preview=True,
        )

    @app.get("/trust/{slug}")
    def trust_public(slug: str, request: Request, conn: Conn):
        page = conn.execute(
            "SELECT * FROM trust_pages WHERE slug = ? AND published = 1", (slug.lower(),)
        ).fetchone()
        if page is None:
            raise HTTPException(status_code=404, detail="Not found")
        return render(
            request,
            "trust_public.html",
            page=page,
            c=content(conn, page["engagement_id"], page),
            rights_days=90,
        )
