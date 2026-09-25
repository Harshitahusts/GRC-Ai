"""Demo mode: a stand-in for the Claude API, for testing without an API key.

Set GRC_AI_MODE=demo. Every AI feature then runs end to end with scripted,
clearly labelled responses in exactly the shape the real API returns: the
assistant calls the real tools, and assessment drafts are structured JSON that
go through the same citation checks. Nothing here is AI output, so the web app
refuses to deliver an engagement with demo findings.
"""

from __future__ import annotations

import json
import re
from itertools import count
from types import SimpleNamespace
from typing import Any

DEMO_PREFIX = "[Demo mode, not AI]"

_ids = count(1)
_STOP = set(
    "a an and are can do does for how i in is it me my of on or "
    "the to we what which who why with".split()
)


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def _tool_use(name: str, tool_input: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        type="tool_use", id=f"demo_tool_{next(_ids)}", name=name, input=tool_input
    )


def _response(stop_reason: str, *content: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(stop_reason=stop_reason, content=list(content), stop_details=None)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        text = block.get("text") if isinstance(block, dict) else getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


class _Messages:
    def create(self, **kwargs: Any) -> SimpleNamespace:
        fmt = (kwargs.get("output_config") or {}).get("format")
        if fmt and fmt.get("type") == "json_schema":
            return self._draft_finding(_content_text(kwargs["messages"][-1]["content"]))
        return self._chat(kwargs["messages"], {t["name"] for t in kwargs.get("tools", [])})

    # -- assessment drafts (ai_assessment.ClaudeAssessor)
    def _draft_finding(self, prompt: str) -> SimpleNamespace:
        provisions = re.findall(r"^\[([^\]]+)\]", prompt, re.M)
        obligation = re.search(r"^OBLIGATION (\S+)[^\n]*\n(.+)$", prompt, re.M)
        status = re.search(r"^STATUS\n(\w[\w ]*)", prompt, re.M)
        ob_id, ob_text = (obligation[1], obligation[2]) if obligation else ("?", "")
        status_word = status[1].split(":")[0].strip().lower() if status else "unknown"
        data = {
            "finding": (
                f"{DEMO_PREFIX} Placeholder finding for {ob_id} ({status_word}): {ob_text} "
                "A real run explains this from the client's answers and the cited provision."
            ),
            "citations": provisions[:1],  # the governing provision is listed first
            "remediation": ""
            if status_word == "compliant"
            else f"{DEMO_PREFIX} Placeholder next step.",
            "confidence": "low",
            "needs_legal_review": False,
        }
        return _response("end_turn", _text(json.dumps(data)))

    # -- analyst chat (agent.Agent)
    def _chat(self, messages: list[dict[str, Any]], tools: set[str]) -> SimpleNamespace:
        last = messages[-1]
        results = [
            b
            for b in (last["content"] if isinstance(last["content"], list) else [])
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        if results:
            return _response("end_turn", _text(self._summarize(results)))

        question = _content_text(last["content"])
        q = question.lower()
        say = lambda msg: _text(f"{DEMO_PREFIX} {msg}")  # noqa: E731
        if re.search(r"\b(gdpr|iso ?27001|soc ?2|nist|hipaa|pci)\b", q):
            return _response(
                "end_turn",
                say("This workspace covers India's DPDP Act and Rules only for now."),
            )
        risk = re.search(r"likelihood\D{0,20}(\d)\D+impact\D{0,20}(\d)", question, re.I)
        if risk and "score_risk" in tools:
            return _response(
                "tool_use",
                say("Scoring that risk on the 5x5 matrix."),
                _tool_use("score_risk", {"likelihood": int(risk[1]), "impact": int(risk[2])}),
            )
        eng = re.search(r"\bENG-0*(\d+)\b", question, re.I)
        if eng and "get_engagement" in tools:
            eid = int(eng[1])
            if re.search(r"evidence|request|fieldwork|collect", q):
                calls = [("get_evidence", {"engagement_id": eid})]
            elif re.search(r"flow|india|transfer|vendor|abroad|leave|processor", q):
                calls = [("get_data_flow", {"engagement_id": eid})]
            elif re.search(r"risk|threat|treat|likelihood|impact", q):
                calls = [("get_risk_register", {"engagement_id": eid})]
            elif re.search(r"finding|gap|open item|issue", q):
                status = "gap" if "gap" in q else "all"
                calls = [("get_findings", {"engagement_id": eid, "status": status})]
            else:  # report, summary, status: the overview and the risks together
                calls = [
                    ("get_engagement", {"engagement_id": eid}),
                    ("get_risk_register", {"engagement_id": eid}),
                ]
            names = ", ".join(n for n, _ in calls)
            return _response(
                "tool_use",
                say(f"Looking this up in the workspace ({names})."),
                *[_tool_use(n, args) for n, args in calls],
            )
        if "list_engagements" in tools and re.search(
            r"client|engagement|portfolio|today|queue|priorit|workload|all\b", q
        ):
            return _response(
                "tool_use",
                say("Checking every engagement in the workspace."),
                _tool_use("list_engagements", {}),
            )
        section = re.search(r"\b(section|rule)\s+(\d+(?:\(\w+\))*)", question, re.I)
        if section and "get_provision" in tools:
            ref = f"{section[1].title()} {section[2]}"
            return _response("tool_use", _tool_use("get_provision", {"ref": ref}))
        words = [w for w in re.findall(r"[a-z]+", q) if w not in _STOP and len(w) > 3]
        if words and "search_obligations" in tools:
            return _response(
                "tool_use",
                say("Searching the DPDPA obligations register."),
                _tool_use("search_obligations", {"query": words[-1]}),
            )
        return _response(
            "end_turn",
            say(
                'Demo mode can only run the tools. Try "What should I work on today?", '
                '"Summarise ENG-001", or "What does DPDPA say about consent?". Add an '
                "ANTHROPIC_API_KEY for real answers."
            ),
        )

    def _summarize(self, results: list[dict[str, Any]]) -> str:
        lines = [f"{DEMO_PREFIX} Here's what the tools returned:"]
        for r in results:
            content = r.get("content", "")
            if r.get("is_error"):
                lines.append(f"- Error: {content}")
                continue
            try:
                data = json.loads(content)
            except (TypeError, json.JSONDecodeError):
                lines.append(f"- {content}")
                continue
            lines += _describe(data)
        lines.append("Add an ANTHROPIC_API_KEY for real, reasoned answers.")
        return "\n".join(lines)


def _describe(data: Any) -> list[str]:
    """Plain bullet points for a tool result, by its kind."""
    kind = data.get("kind") if isinstance(data, dict) else None
    if kind == "risk_score":
        return [
            f"- Risk score {data['score']} (likelihood {data['likelihood']} x impact "
            f"{data['impact']}): {data['level']}."
        ]
    if kind == "obligations":
        out = [f"- {len(data['results'])} matching obligation(s) in register {data['register']}:"]
        if not data["results"]:
            out = ["- No matching obligations in the DPDPA register."]
        for o in data["results"][:5]:
            out.append(
                f"  - {o['id']} ({o['provision']}, {o['severity']}): {o['obligation']} "
                f"Evidence to ask for: {o['evidence_to_request']}"
            )
        return out
    if kind == "provision":
        return [f"- {t['heading']}: {t['text'][:300]}" for t in data["text"][:2]]
    if kind == "engagements":
        out = []
        for e in data["engagements"]:
            top = f"; top risk: {e['top_risk']}" if e["top_risk"] else ""
            ready = e["readiness"] if e["readiness"] is not None else "not assessed"
            out.append(
                f"- {e['client']} (ENG-{e['engagement_id']:03d}): {e['stage']}, readiness "
                f"{ready}, {e['gaps']} gaps, {e['open_risks']} open risks{top}."
            )
        return out or ["- No agent-assisted engagements yet."]
    if kind == "engagement":
        reviewed = sum(d["reviewed"] for d in data["documents"])
        stale = " The assessment is out of date." if data["stale_assessment"] else ""
        return [
            f"- {data['client']} ({data['sector']}): {data['stage']}, readiness "
            f"{data['readiness'] if data['readiness'] is not None else 'not assessed'}.{stale}",
            f"- {len(data['intake'])} intake answers, {len(data['connectors'])} connected "
            f"system(s), {reviewed}/{len(data['documents'])} documents reviewed.",
        ]
    if kind == "findings":
        out = [f"- {len(data['findings'])} finding(s) for {data['client']} ({data['status']}):"]
        for f in data["findings"][:6]:
            out.append(
                f"  - {f['obligation_id']} {f['status'].replace('_', ' ')} ({f['severity']}): "
                f"{f['summary']} Fix: {f['remediation'] or 'n/a'}"
            )
        return out
    if kind == "risks":
        s = data["summary"]
        out = [
            f"- {data['client']}: {s['open']} open risks ({s['by_level']['critical']} critical, "
            f"{s['by_level']['high']} high), {s['overdue']} overdue, {s['unowned']} without "
            "an owner."
        ]
        for r in [r for r in data["risks"] if r["status"] != "closed"][:5]:
            out.append(
                f"  - {r['score']} {r['level']}: {r['title']} Threat: {r['threat']}. "
                f"Treatment: {r['treatment']}."
            )
        return out
    if kind == "dataflow":
        outside = [s["name"] for s in data["systems"] if s["location"] == "Outside India"]
        where = (
            "leaves India via " + ", ".join(outside) if data["leaves_india"] else "stays in India"
        )
        out = [f"- {data['client']}: personal data ({', '.join(data['personal_data'])}) {where}."]
        for p in data["mitigation_plan"][:3]:
            out.append(
                f"  - {p['level']}: {p['issue']} ({', '.join(p['where'])}). Fix: {p['action']}"
            )
        return out
    if kind == "evidence":
        out = [
            f"- {data['client']}: {len(data['collected_by_connectors'])} check(s) collected by "
            f"connectors, {len(data['still_to_request'])} item(s) still to request:"
        ]
        for e in data["still_to_request"][:5]:
            out.append(f"  - {e['obligation_id']} ({e['provision']}): {e['request_from_client']}")
        return out
    return [f"- {json.dumps(data)[:300]}"]


class DemoClient:
    """Duck-types the parts of anthropic.Anthropic the app uses: client.beta.messages.create."""

    api_key = None

    def __init__(self) -> None:
        self.beta = SimpleNamespace(messages=_Messages())
