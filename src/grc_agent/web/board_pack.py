"""The Board inquiry pack: everything about one client in a single ZIP.

If the Data Protection Board inquires into a complaint or a breach (Sections 27-28), the
organisation has to show what it did. This gathers the record GRC Flow already holds: the
obligations register, controls, every register, findings, the data inventory and map,
reviewed documents, the evidence files themselves and the audit trail, with a cover note
saying what is in it and when it was made.

Also: a draft of the detailed breach report Rule 7(2)(b) asks for within 72 hours.
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import zipfile
from pathlib import Path

from grc_agent import obligations as cat
from grc_agent.web import db
from grc_agent.web.dataflow_views import safe_cell
from grc_agent.web.registers import REGISTERS

# Evidence files are added until this much; past it the index still lists every file.
MAX_EVIDENCE_BYTES = 200 * 1024 * 1024


def _csv(header: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(header)
    for r in rows:
        out.writerow([safe_cell("" if v is None else v) for v in r])
    return buf.getvalue()


def _safe_name(text: str, limit: int = 60) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "-" for c in text).strip("-.")[:limit]


def build(conn: sqlite3.Connection, app, eng: sqlite3.Row, user: str) -> bytes:
    from grc_agent.documents import Block, to_docx
    from grc_agent.web import obligation_views, ops_views, register_views

    eid = eng["id"]
    items = obligation_views.items_for(conn, eid)
    sm = obligation_views.summary(items)
    exposure = cat.exposure(items)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # Obligations register
        z.writestr(
            "01-obligations-register.csv",
            _csv(
                [
                    "Ref",
                    "Duty",
                    "Status",
                    "Reviewed",
                    "Owner",
                    "Note",
                    "Re-check",
                    "Evidence files",
                    "Applies from",
                    "Max penalty",
                ],
                [
                    [
                        i["item"].ref,
                        i["item"].title,
                        i["label"],
                        "yes" if i["reviewed"] else "suggested",
                        i["owner"],
                        i["note"],
                        i["review_date"],
                        i["files"],
                        i["item"].applies_from,
                        i["penalty_max"],
                    ]
                    for i in items
                ],
            ),
        )
        # Controls
        controls = ops_views.controls_for(conn, app, eid)
        z.writestr(
            "02-controls.csv",
            _csv(
                [
                    "Obligation",
                    "Provision",
                    "Status",
                    "Owner",
                    "How it's done",
                    "Not applicable because",
                    "Review date",
                    "Evidence files",
                    "Passing connector checks",
                    "Open tasks",
                ],
                [
                    [
                        c["o"].id,
                        c["o"].source,
                        c["label"],
                        c["owner"],
                        c["notes"],
                        c["na_reason"],
                        c["review_date"],
                        c["files"],
                        c["checks"],
                        c["open_tasks"],
                    ]
                    for c in controls
                ],
            ),
        )
        # Registers
        for key, spec in REGISTERS.items():
            recs = register_views.records(conn, eid, spec, "all")
            if not recs:
                continue
            z.writestr(
                f"03-registers/{key}.csv",
                _csv(
                    ["Ref", "Status", "Owner", spec.due_label, "Obligation"]
                    + [f.label for f in spec.fields],
                    [
                        [r["ref"], r["status_label"], r["owner"], r["due"], r["obligation_id"]]
                        + [r["data"].get(f.name, "") for f in spec.fields]
                        for r in recs
                    ],
                ),
            )
        # Assessment findings
        z.writestr(
            "04-assessment-findings.csv",
            _csv(
                [
                    "Obligation",
                    "Status",
                    "Severity",
                    "Citation",
                    "Summary",
                    "Remediation",
                    "Review",
                ],
                [
                    [
                        f["obligation_id"],
                        f["status"],
                        f["severity"],
                        f["citation"],
                        f["summary"],
                        f["remediation"],
                        f["verdict"] or "",
                    ]
                    for f in conn.execute(
                        "SELECT * FROM findings WHERE engagement_id = ? ORDER BY id", (eid,)
                    )
                ],
            ),
        )
        # Record of processing: the data inventory and the data-flow map
        z.writestr(
            "05-data-inventory.csv",
            _csv(
                [
                    "Source",
                    "Field",
                    "Kind",
                    "Category",
                    "Risk",
                    "Children",
                    "Purpose",
                    "Data Principals",
                    "Legal basis",
                    "Retention",
                    "Stored in",
                    "Shared with",
                    "Owner",
                ],
                [
                    [
                        r["source_name"],
                        r["field"],
                        r["kind"],
                        r["category"],
                        r["risk"],
                        "yes" if r["children"] else "no",
                        r["purpose"],
                        r["principals"],
                        r["legal_basis"],
                        r["retention"],
                        r["storage_location"],
                        r["recipients"],
                        r["owner"],
                    ]
                    for r in conn.execute(
                        "SELECT * FROM data_inventory WHERE engagement_id = ? ORDER BY id", (eid,)
                    )
                ],
            ),
        )
        z.writestr(
            "06-data-flow.csv",
            _csv(
                ["System", "Stage", "Location", "Data categories", "Source"],
                [
                    [r["name"], r["stage"], r["location"], r["categories"], r["source"]]
                    for r in conn.execute(
                        "SELECT * FROM dataflow_nodes WHERE engagement_id = ? ORDER BY id", (eid,)
                    )
                ],
            ),
        )
        # Reviewed documents (unreviewed drafts are left out, as on the Documents tab)
        for d in conn.execute(
            "SELECT * FROM documents WHERE engagement_id = ? AND reviewed_at IS NOT NULL "
            "ORDER BY id",
            (eid,),
        ):
            blocks = [Block(**b) for b in json.loads(d["content_json"])]
            z.writestr(f"07-documents/{d['id']}-{_safe_name(d['type'])}.docx", to_docx(blocks))
        # Evidence: an index of every current file, and the files themselves
        files = conn.execute(
            "SELECT * FROM evidence_files WHERE engagement_id = ? AND status = 'current' "
            "ORDER BY id",
            (eid,),
        ).fetchall()
        folder = ops_views.evidence_dir(app)
        added, index = 0, []
        for f in files:
            path = folder / f["stored_name"]
            stem, ext = _safe_name(Path(f["filename"]).stem), Path(f["stored_name"]).suffix
            name = f"08-evidence/{f['id']}-{stem}{ext}"
            included = path.is_file() and added + f["size_bytes"] <= MAX_EVIDENCE_BYTES
            if included:
                z.write(path, name)
                added += f["size_bytes"]
            index.append(
                [
                    f["id"],
                    f["title"],
                    f["category"],
                    f["obligation_id"],
                    f["filename"],
                    f["version"],
                    f["sha256"],
                    f["uploaded_by"],
                    f["uploaded_at"],
                    f["review_date"],
                    name
                    if included
                    else "not included (size limit or missing); download from GRC Flow",
                ]
            )
        z.writestr(
            "08-evidence/index.csv",
            _csv(
                [
                    "ID",
                    "Title",
                    "Kind",
                    "Obligation",
                    "File",
                    "Version",
                    "SHA-256",
                    "Uploaded by",
                    "Uploaded at",
                    "Review by",
                    "In this pack",
                ],
                index,
            ),
        )
        # Audit trail for this client
        z.writestr(
            "09-audit-log.csv",
            _csv(
                ["At", "User", "Action", "Detail"],
                [
                    [r["at"], r["username"], r["action"], r["detail"]]
                    for r in conn.execute(
                        "SELECT at, username, action, detail FROM audit_log "
                        "WHERE engagement_id = ? ORDER BY id",
                        (eid,),
                    )
                ],
            ),
        )
        z.writestr("00-README.txt", _cover(eng, user, sm, exposure, len(files), len(index)))
    return buf.getvalue()


def _cover(eng, user: str, sm: dict, exposure: dict, n_files: int, n_index: int) -> str:
    lines = [
        f"Board inquiry pack: {eng['client']}",
        "=" * 60,
        f"Made {db.now()} by {user} with GRC Flow.",
        "",
        "What this is",
        "  The organisation's own record of how it meets the Digital Personal Data",
        "  Protection Act 2023 and the DPDP Rules 2025, gathered for an inquiry by the",
        "  Data Protection Board of India. It is a self-assessment, not a certification.",
        "",
        "Where things stand",
        f"  Readiness: {sm['pct']}% (partly met duties count half)",
        f"  Duties that apply: {sm['applicable']} of {sm['total']}",
        f"  Compliant: {sm['compliant']} · partly: {sm['partial']} · not: {sm['non_compliant']}"
        f" · not assessed: {sm['not_assessed']}",
        "",
    ]
    if exposure["rows"]:
        lines.append(
            "Duties not fully met, by penalty row in the Act's Schedule (maximum per breach)"
        )
        for r in exposure["rows"]:
            lines.append(f"  {cat.crore(r['amount']):>14}  {r['label']}")
        lines.append("")
    lines += [
        "Contents",
        "  01-obligations-register.csv  every duty, status, owner, evidence count",
        "  02-controls.csv              status of each assessed obligation",
        "  03-registers/                consent, requests, breaches, erasure, vendors, DPIAs,",
        "                               policies, systems, drills, accepted gaps, tasks",
        "  04-assessment-findings.csv   the gap assessment",
        "  05-data-inventory.csv        record of personal data processed",
        "  06-data-flow.csv             systems on the data-flow map",
        "  07-documents/                reviewed documents (notice, policies, reports)",
        f"  08-evidence/                 {n_files} current evidence files; index.csv lists all"
        f" {n_index} with SHA-256 hashes",
        "  09-audit-log.csv             every change made in GRC Flow for this client",
        "",
        "Check the evidence hashes against GRC Flow's records to show nothing was altered.",
    ]
    return "\n".join(lines) + "\n"


def breach_report(eng, rec: sqlite3.Row) -> str:
    """A draft of the detailed report to the Board (Rule 7(2)(b)), from the breach record.
    Gaps are marked so nobody sends it half-filled."""
    d = json.loads(rec["data_json"] or "{}")

    def val(key: str) -> str:
        return (d.get(key) or "").strip() or "[TO FILL IN]"

    types = dict(REGISTERS["breaches"].field("breach_type").choices)
    return (
        "\n".join(
            [
                "DRAFT: Detailed report of a personal data breach",
                "To: Data Protection Board of India",
                f"From: {eng['client']}",
                f"Our reference: {rec['ref']}",
                "",
                "Under Section 8(6) of the Digital Personal Data Protection Act 2023 and",
                "Rule 7(2)(b) of the DPDP Rules 2025. Check every section against the Rules",
                "before sending.",
                "",
                "1. What happened",
                f"   {rec['title']}",
                f"   Type: {types.get(d.get('breach_type', ''), '[TO FILL IN]')}",
                f"   We became aware at: {val('aware_at')}",
                f"   Personal data affected: {val('data_affected')}",
                f"   People affected (estimate): {val('affected_count')}",
                "",
                "2. Updated facts, circumstances and reasons for the breach",
                f"   {val('lessons')}",
                "",
                "3. Measures taken to contain it and reduce the risk",
                f"   {val('containment')}",
                "",
                "4. Any findings about the person who caused it",
                "   [TO FILL IN]",
                "",
                "5. Measures to stop it happening again",
                "   [TO FILL IN]",
                "",
                "6. Notices given",
                f"   Board told (initial intimation) at: {val('board_intimated_at')}",
                f"   Affected people told at: {val('principals_told_at')}",
                "   [Attach a copy of what the affected people were told.]",
                "",
                "Contact: [name, role, email and phone of the person who can answer questions]",
                "",
                "Due: within 72 hours of becoming aware "
                f"({rec['due'] or 'set the awareness time'}).",
            ]
        )
        + "\n"
    )
