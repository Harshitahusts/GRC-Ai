"""The GRC Analyst's view of the workspace: tools over the app's own data.

Each tool opens its own short database connection (the agent runs in a worker
thread) and returns plain JSON, so the analyst reasons over the same
engagements, findings, risks, evidence and data flows the consultant sees.

Every tool reads, except one action: create_task, offered only to people who may edit
(not viewers). It drafts a task in the Tasks register for a person to check, labelled
as drafted by the analyst, recorded under the person who asked, and never touches a
finding, a control or a delivery decision. Those stay with people.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import FastAPI

from grc_agent import dataflow, risk
from grc_agent.tools import BASE_TOOLS, Tool, ToolError
from grc_agent.web import connector_views, db


def risks_for(conn: sqlite3.Connection, app: FastAPI, eid: int) -> list[risk.Risk]:
    from grc_agent.web.app import findings_of

    edits = {
        r["risk_key"]: dict(r)
        for r in conn.execute("SELECT * FROM risk_edits WHERE engagement_id = ?", (eid,))
    }
    return risk.build(
        eid,
        [dict(f) for f in findings_of(conn, eid)],
        {o.id: o for o in app.state.register.obligations},
        connector_views.evidence_rows(conn, eid),
        edits,
    )


def flow_for(conn: sqlite3.Connection, app: FastAPI, eng: sqlite3.Row) -> dict[str, Any]:
    from grc_agent.web.app import answers_of, findings_of

    eid = eng["id"]
    custom = conn.execute("SELECT * FROM dataflow_nodes WHERE engagement_id = ?", (eid,))
    return dataflow.build(
        eid,
        answers_of(conn, eid),
        [dict(f) for f in findings_of(conn, eid)],
        {o.id: o for o in app.state.register.obligations},
        connector_views.evidence_rows(conn, eid),
        [dict(c) for c in custom],
        assessed=bool(eng["assessed_at"]),
    )


def _engagement(conn: sqlite3.Connection, eid: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM engagements WHERE id = ?", (eid,)).fetchone()
    if row is None:
        raise ToolError(f"No engagement with id {eid}. Call list_engagements for valid ids.")
    return row


def analyst_tools(app: FastAPI, user: str = "", can_act: bool = False) -> list[Tool]:
    from grc_agent.web.app import _summary, answers_of, documents_of, findings_of

    obligations = {o.id: o for o in app.state.register.obligations}
    questions = {q.id: q.text for q in app.state.register.questions}

    def connect() -> sqlite3.Connection:
        return db.connect(app.state.db_path)

    def list_engagements() -> dict[str, Any]:
        with connect() as conn:
            rows = conn.execute("SELECT * FROM engagements ORDER BY id")
            out = []
            for e in rows.fetchall():
                s = _summary(conn, e)
                risks = [r for r in risks_for(conn, app, e["id"]) if r.status != "closed"]
                out.append(
                    {
                        "engagement_id": e["id"],
                        "client": e["client"],
                        "sector": e["sector"],
                        "stage": s["stage"],
                        "readiness": s["score"],
                        "gaps": s["gaps"],
                        "open_items": s["open_items"],
                        "documents_reviewed": f"{s['reviewed']}/{s['documents']}",
                        "open_risks": len(risks),
                        "top_risk": risks[0].title if risks else None,
                    }
                )
        return {"kind": "engagements", "engagements": out}

    def get_engagement(engagement_id: int) -> dict[str, Any]:
        with connect() as conn:
            e = _engagement(conn, engagement_id)
            s = _summary(conn, e)
            answers = answers_of(conn, engagement_id)
            connectors = conn.execute(
                "SELECT connector, status, message, last_synced_at FROM connections "
                "WHERE engagement_id = ?",
                (engagement_id,),
            ).fetchall()
            docs = documents_of(conn, engagement_id)
        return {
            "kind": "engagement",
            "engagement_id": engagement_id,
            "client": e["client"],
            "sector": e["sector"],
            "stage": s["stage"],
            "readiness": s["score"],
            "stale_assessment": s["stale"],
            "delivered": s["delivered"],
            "intake": [
                {"question": questions.get(k, k), "answer": v} for k, v in sorted(answers.items())
            ],
            "connectors": [dict(c) for c in connectors],
            "documents": [
                {"type": d["type"], "reviewed": bool(d["reviewed_at"]), "outcome": d["outcome"]}
                for d in docs
            ],
        }

    def get_findings(engagement_id: int, status: str) -> dict[str, Any]:
        with connect() as conn:
            e = _engagement(conn, engagement_id)
            rows = [dict(f) for f in findings_of(conn, engagement_id)]
        if status != "all":
            rows = [f for f in rows if f["status"] == status]
        out = []
        for f in rows:
            o = obligations.get(f["obligation_id"])
            out.append(
                {
                    "obligation_id": f["obligation_id"],
                    "obligation": o.obligation if o else "",
                    "provision": o.source if o else f["citation"],
                    "status": f["status"],
                    "severity": f["severity"],
                    "summary": f["summary"],
                    "remediation": f["remediation"],
                    "evidence_to_request": o.evidence if o else "",
                    "citation_resolves": bool(f["citation_resolves"]),
                    "verdict": f["verdict"],
                }
            )
        return {"kind": "findings", "client": e["client"], "status": status, "findings": out}

    def get_risk_register(engagement_id: int) -> dict[str, Any]:
        with connect() as conn:
            e = _engagement(conn, engagement_id)
            risks = risks_for(conn, app, engagement_id)
        return {
            "kind": "risks",
            "client": e["client"],
            "summary": risk.summary(risks),
            "risks": [r.to_dict() for r in risks],
        }

    def get_data_flow(engagement_id: int) -> dict[str, Any]:
        with connect() as conn:
            e = _engagement(conn, engagement_id)
            flow = flow_for(conn, app, e)
        names = {n["id"]: n["name"] for n in flow["nodes"]}
        return {
            "kind": "dataflow",
            "client": e["client"],
            "leaves_india": flow["summary"]["leaves_india"],
            "personal_data": flow["categories"],
            "systems": [
                {
                    "name": n["name"],
                    "stage": n["stage"],
                    "location": flow["locations"][n["location"]],
                    "issues": [i["title"] for i in n["issues"]],
                }
                for n in flow["nodes"]
            ],
            "flows": [
                f"{names[x['source']]} -> {names[x['target']]}"
                + (f" ({x['label']})" if x["label"] else "")
                for x in flow["edges"]
            ],
            "mitigation_plan": [
                {
                    "level": p["level"],
                    "where": p["where"],
                    "issue": p["title"],
                    "action": p["action"],
                }
                for p in flow["plan"]
            ],
        }

    def get_evidence(engagement_id: int) -> dict[str, Any]:
        from grc_agent.web.ops_views import COUNTS_AS_EVIDENCE

        with connect() as conn:
            e = _engagement(conn, engagement_id)
            rows = connector_views.evidence_rows(conn, engagement_id)
            findings = [dict(f) for f in findings_of(conn, engagement_id)]
            files = conn.execute(
                # Safe: the SQL text holds only names from this code; values are ? parameters.
                f"SELECT title, category, obligation_id, version, uploaded_at, ai_check, "  # nosec B608  # noqa: S608
                f"ai_check_reason, check_overruled_by, {COUNTS_AS_EVIDENCE} AS counts "
                "FROM evidence_files WHERE engagement_id = ? AND status = 'current' "
                "ORDER BY id",
                (engagement_id,),
            ).fetchall()
        requests = []
        for f in findings:
            o = obligations.get(f["obligation_id"])
            if o and f["status"] in ("gap", "open_item"):
                requests.append(
                    {
                        "obligation_id": o.id,
                        "provision": o.source,
                        "request_from_client": o.evidence,
                        "why": f["summary"],
                    }
                )
        return {
            "kind": "evidence",
            "client": e["client"],
            "collected_by_connectors": [
                {
                    "connector": r["connector"],
                    "check": r["title"],
                    "result": r["status"],
                    "detail": r["detail"],
                    "provisions": r["provisions"],
                    "collected_at": r["collected_at"],
                }
                for r in rows
            ],
            "uploaded_files": [
                {
                    "title": f["title"],
                    "kind": f["category"],
                    "supports": obligations[f["obligation_id"]].source
                    if f["obligation_id"] in obligations
                    else None,
                    "version": f["version"],
                    "uploaded": f["uploaded_at"][:10],
                    # The AI's view of whether the file is on topic; people decide.
                    "ai_relevance_check": f["ai_check"] or "not checked",
                    "ai_check_reason": f["ai_check_reason"] or None,
                    "counts_as_evidence": bool(f["counts"]),
                    "overruled_by": f["check_overruled_by"] or None,
                }
                for f in files
            ],
            "still_to_request": requests,
        }

    def get_readiness_plan(engagement_id: int) -> dict[str, Any]:
        from grc_agent import plan
        from grc_agent.web.ops_views import readiness_plan

        with connect() as conn:
            e = _engagement(conn, engagement_id)
            steps = readiness_plan(conn, app, e)
        return {
            "kind": "plan",
            "client": e["client"],
            "start_here": plan.recommended(steps),
            "steps": [
                {
                    "step": st.key,
                    "title": st.title,
                    "done": f"{st.completed}/{st.total}",
                    "checks": [{"check": label, "done": ok} for label, ok, _ in st.parts],
                    "obligations": [
                        {
                            "obligation_id": i.obligation.id,
                            "provision": i.obligation.source,
                            "control": i.control_label,
                            "assessment": i.finding,
                            "evidence_files": i.files,
                            "flagged_by_ai": i.flagged,
                            "open_tasks": i.open_tasks,
                            "done": i.done,
                            "next_action": i.action,
                        }
                        for i in st.items
                    ],
                }
                for st in steps
            ],
        }

    def create_task(
        engagement_id: int, obligation_id: str, title: str, details: str, priority: str
    ) -> dict[str, Any]:
        from grc_agent.web import register_views
        from grc_agent.web.registers import PRIORITIES, TASKS

        title, details = title.strip()[:160], details.strip()[:2000]
        if not title:
            raise ToolError("Give the task a title.")
        if priority not in {k for k, _ in PRIORITIES}:
            raise ToolError("priority must be low, medium, high or urgent.")
        if obligation_id and obligation_id not in obligations:
            raise ToolError(f"Unknown obligation {obligation_id}; use search_obligations.")
        with connect() as conn:
            _engagement(conn, engagement_id)
            for r in conn.execute(
                "SELECT id, ref, title, status FROM records WHERE engagement_id = ? "
                "AND register = 'tasks'",
                (engagement_id,),
            ).fetchall():
                if r["title"].lower() == title.lower() and r["status"] in TASKS.open_statuses:
                    return {
                        "kind": "task",
                        "created": False,
                        "ref": r["ref"],
                        "note": "An open task with this title already exists; nothing new made.",
                        "link": f"/engagements/{engagement_id}/r/tasks/{r['id']}",
                    }
            rid = register_views.create_record(
                conn,
                engagement_id,
                TASKS,
                {"title": title, "details": details, "priority": priority},
                user,
                obligation_id=obligation_id,
            )
            ref = conn.execute("SELECT ref FROM records WHERE id = ?", (rid,)).fetchone()[0]
            register_views.add_event(
                conn,
                rid,
                engagement_id,
                user,
                "comment",
                {
                    "text": f"Drafted by the GRC Analyst (AI) when {user} asked. "
                    "Check the wording, owner and due date before relying on it."
                },
            )
            db.audit(
                conn,
                user,
                "analyst_created_task",
                engagement_id,
                {"ref": ref, "title": title, "obligation": obligation_id},
            )
        return {
            "kind": "task",
            "created": True,
            "ref": ref,
            "client": e["client"],
            "link": f"/engagements/{engagement_id}/r/tasks/{rid}",
            "note": "Created as a draft for a person to check. Tell the user the reference.",
        }

    eid_schema = {"type": "integer", "description": "From list_engagements."}
    tools = [
        *BASE_TOOLS,
        Tool(
            name="list_engagements",
            description=(
                "List the workspace's client engagements with stage, readiness score (0-100), "
                "gap and open-item counts, documents reviewed, open risks and the top risk. "
                "Start here to find engagement ids."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
                "additionalProperties": False,
            },
            handler=list_engagements,
        ),
        Tool(
            name="get_engagement",
            description=(
                "One engagement's details: stage, readiness, whether the assessment is stale, "
                "the client's intake answers, connected systems and document review status."
            ),
            input_schema={
                "type": "object",
                "properties": {"engagement_id": eid_schema},
                "required": ["engagement_id"],
                "additionalProperties": False,
            },
            handler=get_engagement,
        ),
        Tool(
            name="get_findings",
            description=(
                "An engagement's findings against the DPDPA obligations register: status, "
                "severity, provision, what the client said, the remediation and the evidence "
                "to request. Filter by status."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "engagement_id": eid_schema,
                    "status": {
                        "type": "string",
                        "enum": ["all", "gap", "open_item", "compliant", "not_applicable"],
                    },
                },
                "required": ["engagement_id", "status"],
                "additionalProperties": False,
            },
            handler=get_findings,
        ),
        Tool(
            name="get_risk_register",
            description=(
                "An engagement's DPDPA risk register: each risk's threat, vulnerability, "
                "likelihood and impact (1-5), score and level, treatment, owner, due date and "
                "status, plus totals by level, overdue and unowned counts."
            ),
            input_schema={
                "type": "object",
                "properties": {"engagement_id": eid_schema},
                "required": ["engagement_id"],
                "additionalProperties": False,
            },
            handler=get_risk_register,
        ),
        Tool(
            name="get_data_flow",
            description=(
                "An engagement's personal-data flow: the personal data collected, each system "
                "and vendor with its location (India or outside), the flows between them, "
                "whether data leaves India, and the mitigation plan for issues in the flow."
            ),
            input_schema={
                "type": "object",
                "properties": {"engagement_id": eid_schema},
                "required": ["engagement_id"],
                "additionalProperties": False,
            },
            handler=get_data_flow,
        ),
        Tool(
            name="get_evidence",
            description=(
                "An engagement's evidence: checks collected automatically by connectors (with "
                "results) and the evidence still to request from the client for each gap or "
                "open item."
            ),
            input_schema={
                "type": "object",
                "properties": {"engagement_id": eid_schema},
                "required": ["engagement_id"],
                "additionalProperties": False,
            },
            handler=get_evidence,
        ),
        Tool(
            name="get_readiness_plan",
            description=(
                "An engagement's DPDPA readiness plan: its obligations grouped into steps "
                "(scope, notice and consent, security and breaches, rights, retention, "
                "processors, transfers, accountability, delivery), what is done, where to "
                "start and the next action for each obligation. Worked out from live data."
            ),
            input_schema={
                "type": "object",
                "properties": {"engagement_id": eid_schema},
                "required": ["engagement_id"],
                "additionalProperties": False,
            },
            handler=get_readiness_plan,
        ),
    ]
    if can_act:
        tools.append(
            Tool(
                name="create_task",
                description=(
                    "ACTION: add a task to an engagement's Tasks register, as a draft for a "
                    "person to check. Only when the user asks you to create or add tasks. "
                    "One task per call; give the obligation id it serves when there is one "
                    "(empty string otherwise). Never use it to mark anything done."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "engagement_id": eid_schema,
                        "obligation_id": {
                            "type": "string",
                            "description": "e.g. OBL-005, or empty.",
                        },
                        "title": {"type": "string", "description": "What needs doing, short."},
                        "details": {
                            "type": "string",
                            "description": "Steps, citing the provision.",
                        },
                        "priority": {
                            "type": "string",
                            "enum": ["low", "medium", "high", "urgent"],
                        },
                    },
                    "required": ["engagement_id", "obligation_id", "title", "details", "priority"],
                    "additionalProperties": False,
                },
                handler=create_task,
            )
        )
    return tools


def queue(conn: sqlite3.Connection, app: FastAPI) -> list[dict[str, Any]]:
    """The analyst's to-do list across clients: the top open risks, overdue first.

    Delivered engagements are included: delivery ends the assessment, not the risks.
    """
    out = []
    for e in conn.execute("SELECT * FROM engagements"):
        for r in risks_for(conn, app, e["id"]):
            if r.status == "closed":
                continue
            out.append({"client": e["client"], "eid": e["id"], **r.to_dict()})
    out.sort(key=lambda r: (not r["overdue"], -r["score"]))
    return out
