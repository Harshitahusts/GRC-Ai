"""A client's personal-data flow map, built from what the app already knows.

Sources, in order of trust:
- connector evidence (e.g. AWS shows buckets in us-east-1),
- intake answers (what data is collected, which tools hold it, retention),
- findings (each gap or open item is pinned to the step of the flow it affects),
- systems a consultant adds by hand.

The result is plain data (nodes, edges, issues, plan) that the page draws and
re-fetches, so the map changes as soon as the intake, the assessment or a
connector check does.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

STAGES = (
    ("principals", "Data principals"),
    ("collection", "Collection & requests"),
    ("systems", "Your systems"),
    ("vendors", "Outside companies"),
    ("destinations", "Where data ends up"),
)
STAGE_INDEX = {key: i for i, (key, _) in enumerate(STAGES)}
LEVEL_RANK = {"critical": 0, "serious": 1, "warning": 2}
EVIDENCE_ACTION = "Fix this setting in the client's account, then run the checks again."
LOCATIONS = {"india": "India", "outside": "Outside India", "unknown": "Not known"}

# Where each obligation bites, by the provision it comes from.
_PROVISION_NODE = (
    (r"^Section 9\b", "collect"),
    (r"^Section [56]\b", "collect"),
    (r"^Section 8\(2\)", "vendors"),
    (r"^Section 8\(7\)", "deletion"),
    (r"^Section 8\((9|10)\)", "rights"),
    (r"^Section 1[1-4]\b", "rights"),
    (r"^Section 16\b", "abroad"),
    (r"^Section 8\b", "core"),
)

# Well-known services, to place vendors named in the intake. Anything else is "Not known".
_FOREIGN = (
    "google", "gmail", "workspace", "microsoft", "office 365", "outlook", "azure", "aws", "amazon",
    "mailchimp", "hubspot", "salesforce", "slack", "zoom", "shopify", "stripe", "dropbox", "notion",
    "atlassian", "jira", "github", "zendesk", "intercom", "twilio", "sendgrid", "meta", "facebook",
    "whatsapp", "instagram", "linkedin", "apple", "icloud", "oracle", "xero", "quickbooks", "canva",
    "figma", "trello", "asana", "cloudflare", "mixpanel", "segment", "calendly", "typeform",
)  # fmt: skip
_INDIAN = (
    "zoho", "razorpay", "tally", "keka", "darwinbox", "paytm", "phonepe", "cashfree", "msg91",
    "exotel", "gupshup", "greythr", "payu", "instamojo", "juspay", "setu", "digilocker",
)  # fmt: skip
_SPLIT = re.compile(r"[,;\n/]+|\band\b|\b&\b")
_FILLER = re.compile(r"^(e\.?g\.?|like|such as|our|we use)\s+", re.I)


@dataclass
class Issue:
    node: str
    level: str
    title: str
    detail: str
    action: str
    provision: str
    source: str  # "finding" | "evidence"
    link: str


@dataclass
class Node:
    id: str
    name: str
    stage: str
    location: str = "unknown"
    categories: list[str] = field(default_factory=list)
    note: str = ""
    custom_id: int | None = None
    status: str = "ok"  # ok | pending | warning | serious | critical
    issues: list[Issue] = field(default_factory=list)


@dataclass
class Edge:
    source: str
    target: str
    categories: list[str]
    label: str = ""
    status: str = "ok"


def _items(text: str, limit: int = 10) -> list[str]:
    out: list[str] = []
    for part in _SPLIT.split(text or ""):
        item = _FILLER.sub("", part.strip(" .:-\t"))
        item = item.strip(" .")
        if 1 < len(item) <= 60 and item.lower() not in {x.lower() for x in out}:
            out.append(item[0].upper() + item[1:])
    return out[:limit]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "item"


def vendor_location(name: str) -> str:
    low = name.lower()
    if any(re.search(rf"\b{re.escape(k)}\b", low) for k in _INDIAN):
        return "india"
    if any(re.search(rf"\b{re.escape(k)}\b", low) for k in _FOREIGN):
        return "outside"
    return "unknown"


def node_for_provision(provision: str) -> str:
    for pattern, node in _PROVISION_NODE:
        if re.search(pattern, provision or ""):
            return node
    return "core"


def _finding_level(status: str, severity: str) -> str | None:
    if status == "gap":
        return {"critical": "critical", "high": "serious"}.get(severity, "warning")
    if status == "open_item":
        return "warning"
    return None


def build(
    eid: int,
    answers: dict[str, str],
    findings: list[dict[str, Any]],
    obligations: dict[str, Any],
    evidence: list[dict[str, Any]],
    custom: list[dict[str, Any]],
    assessed: bool,
) -> dict[str, Any]:
    """The flow map for one engagement, as JSON-ready data."""
    yes = lambda q: answers.get(q) in ("yes", "not_sure")  # noqa: E731
    cats = _items(answers.get("INFO-DATA", "")) or ["Personal data"]
    nodes: dict[str, Node] = {}
    edges: list[Edge] = []

    def add(node: Node) -> Node:
        nodes.setdefault(node.id, node)
        return nodes[node.id]

    def link(a: str, b: str, categories: list[str] | None = None, label: str = "") -> None:
        if a in nodes and b in nodes and not any(e.source == a and e.target == b for e in edges):
            edges.append(Edge(a, b, list(categories if categories is not None else cats), label))

    # Who the data is about, and where it comes in.
    purpose = answers.get("INFO-PURPOSE", "")
    contact = answers.get("INFO-CONTACT", "")
    add(Node("customers", "Customers & users", "principals", "india", cats))
    if yes("CTX-CHILDREN"):
        note = "Needs verifiable parental consent."
        add(Node("children", "Children (under 18)", "principals", "india", cats, note=note))
    note = f"Used for: {purpose}" if purpose else ""
    add(Node("collect", "Sign-up & collection points", "collection", "india", cats, note=note))
    note = f"Contact: {contact}" if contact else ""
    add(Node("rights", "Rights & grievance requests", "collection", "india", [], note=note))
    add(Node("core", "Core app & databases", "systems", "unknown", cats))
    link("customers", "collect")
    link("children", "collect")
    link("customers", "rights", [], "Access, correction, complaints")
    link("collect", "core")
    link("rights", "core", [], "Requests handled")

    # Outside companies named at intake.
    vendors = _items(answers.get("INFO-TOOLS", ""), limit=8)
    if not vendors and yes("CTX-VENDORS"):
        vendors = ["Outside companies (not listed yet)"]
    for v in vendors:
        n = add(Node(f"vendor-{_slug(v)}", v, "vendors", vendor_location(v), cats))
        link("core", n.id)

    # Connected systems and what their checks found.
    by_connector: dict[str, list[dict[str, Any]]] = {}
    for e in evidence:
        by_connector.setdefault(e["connector"], []).append(e)
    outside_regions: list[str] = []
    if "aws" in by_connector:
        regions = [r for e in by_connector["aws"] for r in e["data"].get("outside_india", [])]
        inside = [r for e in by_connector["aws"] for r in e["data"].get("inside_india", [])]
        outside_regions = sorted(set(regions))
        loc = "outside" if outside_regions else ("india" if inside else "unknown")
        add(Node("aws", "AWS account", "systems", loc, cats, note="Connected: live evidence"))
        link("core", "aws", label="Hosting & storage")
    if "github" in by_connector:
        note = "Connected: live evidence. Code, config and any secrets in it."
        add(Node("github", "GitHub repositories", "systems", "outside", [], note=note))
        link("core", "github", [], "Code & configuration")

    # Where data ends up.
    retention = answers.get("INFO-RETENTION", "")
    note = f"Client says: {retention}" if retention else "Retention not described yet."
    add(Node("deletion", "Deletion & retention", "destinations", "india", [], note=note))
    link("core", "deletion", [], "End of purpose")

    # Custom systems a consultant added.
    default_source = {
        "principals": "",
        "collection": "customers",
        "systems": "collect",
        "vendors": "core",
        "destinations": "core",
    }
    for c in custom:
        n = add(
            Node(
                f"custom-{c['id']}",
                c["name"],
                c["stage"],
                c["location"],
                _items(c["categories"]),
                note="Added by the consultant",
                custom_id=c["id"],
            )
        )
        source = c["source"] if c["source"] in nodes else default_source.get(c["stage"], "core")
        if source:
            link(source, n.id, n.categories or cats)
        if c["stage"] == "principals":
            link(n.id, "collect", n.categories or cats)

    leaves_india = (
        answers.get("CTX-FOREIGN") in ("yes", "not_sure")
        or bool(outside_regions)
        or any(n.location == "outside" and n.stage != "principals" for n in nodes.values())
    )
    if leaves_india:
        note = "Services run or hosted outside India."
        if outside_regions:
            note = "AWS regions: " + ", ".join(outside_regions)
        add(Node("abroad", "Outside India", "destinations", "outside", cats, note=note))
        for n in list(nodes.values()):
            if n.location == "outside" and n.id != "abroad" and n.stage != "principals":
                link(n.id, "abroad", n.categories or cats, "Cross-border")
        if answers.get("CTX-FOREIGN") in ("yes", "not_sure") and not any(
            e.target == "abroad" for e in edges
        ):
            link("core", "abroad", label="Foreign services (not named)")

    # Pin issues to the step they affect.
    for f in findings:
        level = _finding_level(f["status"], f["severity"])
        if not level:
            continue
        o = obligations.get(f["obligation_id"])
        provision = getattr(o, "source", "") or f.get("citation", "")
        target = node_for_provision(provision)
        targets = [target]
        if target == "vendors":
            targets = [n.id for n in nodes.values() if n.stage == "vendors"] or ["core"]
        elif target not in nodes:
            targets = ["core"]
        for t in targets:
            nodes[t].issues.append(
                Issue(
                    node=t,
                    level=level,
                    title=getattr(o, "obligation", f["obligation_id"]),
                    detail=f["summary"],
                    action=f["remediation"] or "Collect evidence and confirm with the client.",
                    provision=provision,
                    source="finding",
                    link=f"/engagements/{eid}/findings#f{f['id']}",
                )
            )
    for connector, rows in by_connector.items():
        target = connector if connector in nodes else "core"
        for e in rows:
            if e["status"] not in ("fail", "warn"):
                continue
            targets = [target]
            if e["check_key"] == "data_location" and "abroad" in nodes:
                targets.append("abroad")
            for t in targets:
                nodes[t].issues.append(
                    Issue(
                        node=t,
                        level="serious" if e["status"] == "fail" else "warning",
                        title=e["title"],
                        detail=e["detail"],
                        action=EVIDENCE_ACTION,
                        provision=", ".join(e["provisions"]),
                        source="evidence",
                        link=f"/engagements/{eid}/connectors",
                    )
                )

    for n in nodes.values():
        if n.issues:
            n.status = min((i.level for i in n.issues), key=LEVEL_RANK.__getitem__)
        elif not assessed and n.stage != "principals":
            n.status = "pending"
    for e in edges:
        e.status = nodes[e.target].status

    issues = sorted(
        (i for n in nodes.values() for i in n.issues),
        key=lambda i: (LEVEL_RANK[i.level], STAGE_INDEX[nodes[i.node].stage]),
    )
    plan: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for i in issues:  # one plan step per problem, even if it shows on several vendors
        key = (i.title, i.action)
        if key in seen:
            step = next(p for p in plan if (p["title"], p["action"]) == key)
            step["where"].append(nodes[i.node].name)
            continue
        seen.add(key)
        plan.append({**asdict(i), "where": [nodes[i.node].name]})

    all_categories = sorted({c for e in edges for c in e.categories}, key=str.lower)
    data = {
        "stages": [{"key": k, "label": label} for k, label in STAGES],
        "nodes": [asdict(n) for n in sorted(nodes.values(), key=lambda n: STAGE_INDEX[n.stage])],
        "edges": [asdict(e) for e in edges],
        "plan": plan,
        "categories": all_categories,
        "summary": {
            "systems": sum(1 for n in nodes.values() if n.stage in ("systems", "vendors")),
            "flows": len(edges),
            "issues": {lvl: sum(1 for i in issues if i.level == lvl) for lvl in LEVEL_RANK},
            "leaves_india": leaves_india,
            "assessed": assessed,
        },
        "locations": LOCATIONS,
    }
    data["version"] = hashlib.sha1(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]
    return data
