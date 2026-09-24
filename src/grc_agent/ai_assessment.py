"""Claude-drafted findings (ticket H6), built on the deterministic assessment.

Order of a run, per the build plan (Step 9.2):
1. Rules decide applicability, status and severity. No model call.
2. For each obligation that applies (or might), pull the governing provisions:
   the one the register names, plus the closest matches from search.
3. Claude gets that obligation, those provisions, and only the intake answers
   tied to it, and drafts the finding as structured JSON.
4. Deterministic checks: malformed output is retried once, then the run fails.
   Every citation must resolve in the corpus index AND come from the provisions
   Claude was shown. Anything else is recorded as unresolved and blocks delivery.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import anthropic

from grc_agent.assessment import AssessedFinding, assess
from grc_agent.config import Settings
from grc_agent.corpus.ingest import Chunk
from grc_agent.corpus.store import Corpus
from grc_agent.kpis.citations import normalize_citation
from grc_agent.register import CHOICES, Obligation, Register

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_PROVISIONS = 5
SEARCH_EXTRA = 3

SYSTEM_PROMPT = """\
You draft findings for a DPDPA (India's Digital Personal Data Protection Act 2023 and
Rules 2025) gap assessment. A consultant reviews every finding before a client sees it.

For each request you get one obligation, its status (already decided by rules from the
client's answers), the client's answers to the questions tied to it, and the provisions
retrieved from the Act and Rules.

- Write the finding in plain English for a business owner: what the obligation requires,
  what the client's answers show, and why it matters. Two to four sentences.
- Cite only provisions from the list you're given, written exactly as they are labelled
  there, such as "Section 8(6)". Cite the most specific provision that supports the finding.
  Never cite anything that isn't in the list.
- Don't change the status. If the answers look inconsistent with it, say so in the finding.
- Remediation: concrete next steps for this client, or an empty string if compliant.
- Confidence: "high" when the provisions clearly cover the obligation and the answers are
  clear; "low" when the provisions don't clearly support it or the facts are thin.
- needs_legal_review: true when the finding turns on legal interpretation, an exemption,
  or a conflict with another law.
- If the provisions don't support the obligation, say so plainly. An honest "insufficient
  information" is better than a confident guess."""

FINDING_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "finding": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "string"}},
        "remediation": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
        "needs_legal_review": {"type": "boolean"},
    },
    "required": ["finding", "citations", "remediation", "confidence", "needs_legal_review"],
    "additionalProperties": False,
}

STATUS_TEXT = {
    "gap": "GAP: the client says this is not in place.",
    "compliant": "COMPLIANT: the client says this is in place (evidence not yet checked).",
    "open_item": "OPEN ITEM: the client skipped the question or wasn't sure.",
}


class AssessmentError(RuntimeError):
    """The model's output couldn't be used, even after a retry. Nothing is saved."""


def gather_provisions(corpus: Corpus, obligation: Obligation) -> list[Chunk]:
    chunks = list(corpus.provision(obligation.source))
    for hit in corpus.search(obligation.obligation, SEARCH_EXTRA):
        if hit.chunk not in chunks:
            chunks.append(hit.chunk)
    return chunks[:MAX_PROVISIONS]


def relevant_answers(
    register: Register, obligation: Obligation, answers: dict[str, str]
) -> list[str]:
    """Only the answers tied to this obligation (data minimisation, Step 9.3)."""
    ids = [obligation.question]
    if obligation.applies_if:
        ids.insert(0, obligation.applies_if.question)
    out = []
    for qid in ids:
        q = register.question(qid)
        raw = answers.get(qid)
        answer = CHOICES.get(raw, raw) if raw else "(skipped)"
        out.append(f"Q: {q.text}\nA: {answer}")
    return out


def build_prompt(
    finding: AssessedFinding, obligation: Obligation, answers: list[str], provisions: list[Chunk]
) -> str:
    provision_text = "\n\n".join(
        f"[{c.ref}] {c.heading}\n{c.text}" if c.heading else f"[{c.ref}]\n{c.text}"
        for c in provisions
    )
    return (
        f"OBLIGATION {obligation.id} (register source: {obligation.source}, "
        f"severity: {finding.severity})\n{obligation.obligation}\n\n"
        f"STATUS\n{STATUS_TEXT[finding.status]}\n\n"
        f"CLIENT ANSWERS\n" + "\n\n".join(answers) + "\n\n"
        f"EVIDENCE THAT WOULD SHOW COMPLIANCE\n{obligation.evidence}\n\n"
        f"PROVISIONS (cite only these)\n{provision_text}"
    )


def _grounded(ref: str, provisions: list[Chunk]) -> bool:
    """The citation is one of the provisions shown, a clause inside one, or its parent."""
    key = normalize_citation(ref)
    if key is None:
        return False
    return any(
        key == c.key or key.startswith(c.key + "(") or c.key.startswith(key + "(")
        for c in provisions
    )


def _parse(response: Any) -> dict[str, Any]:
    if response.stop_reason == "refusal":
        raise AssessmentError("Claude declined this request.")
    if response.stop_reason == "max_tokens":
        raise ValueError("output was cut off")
    text = next((b.text for b in response.content if b.type == "text"), "")
    data = json.loads(text)
    missing = [k for k in FINDING_SCHEMA["required"] if k not in data]
    if missing:
        raise ValueError(f"missing fields: {', '.join(missing)}")
    if not isinstance(data["citations"], list) or not data["citations"]:
        raise ValueError("no citations")
    if not str(data["finding"]).strip():
        raise ValueError("empty finding")
    return data


class ClaudeAssessor:
    def __init__(
        self,
        corpus: Corpus,
        client: anthropic.Anthropic | None = None,
        settings: Settings | None = None,
        workers: int = 4,
    ) -> None:
        self.settings = settings or Settings.from_env()
        self.client = client or anthropic.Anthropic()
        self.corpus = corpus
        self.workers = workers

    def _call(self, prompt: str) -> Any:
        return self.client.beta.messages.create(
            model=self.settings.model,
            max_tokens=self.settings.max_tokens,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
            thinking={"type": "adaptive"},
            output_config={
                "effort": self.settings.effort,
                "format": {"type": "json_schema", "schema": FINDING_SCHEMA},
            },
            cache_control={"type": "ephemeral"},
            fallbacks="default",
            betas=[FALLBACK_BETA],
        )

    def draft(
        self, finding: AssessedFinding, obligation: Obligation, register: Register, answers: dict
    ) -> AssessedFinding:
        provisions = gather_provisions(self.corpus, obligation)
        if not provisions:
            raise AssessmentError(
                f"{obligation.id}: no provisions found for {obligation.source}. "
                "Is the corpus built from the right sources?"
            )
        prompt = build_prompt(
            finding, obligation, relevant_answers(register, obligation, answers), provisions
        )
        last_error = ""
        for _ in range(2):  # one retry on malformed output, then a hard fail
            try:
                data = _parse(self._call(prompt))
                break
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = str(exc)
        else:
            raise AssessmentError(f"{obligation.id}: unusable output after a retry ({last_error}).")

        citations = tuple(dict.fromkeys(str(c).strip() for c in data["citations"]))
        unresolved = tuple(
            c
            for c in citations
            if not self.corpus.index.resolves(c) or not _grounded(c, provisions)
        )
        return replace(
            finding,
            citations=citations,
            unresolved=unresolved,
            summary=data["finding"].strip(),
            remediation=data["remediation"].strip() if finding.status != "compliant" else "",
            drafted_by="claude",
            confidence=data["confidence"],
            needs_legal_review=bool(data["needs_legal_review"]),
            provisions=tuple(c.ref for c in provisions),
        )

    def assess(self, register: Register, answers: dict[str, str]) -> list[AssessedFinding]:
        """Rules first, then Claude drafts every finding that isn't not_applicable.

        All-or-nothing: if any obligation fails, AssessmentError is raised and the
        caller keeps whatever it had before.
        """
        base = assess(register, answers, self.corpus.index)
        obligations = {o.id: o for o in register.obligations}
        todo = [f for f in base if f.status != "not_applicable"]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            drafted = list(
                pool.map(
                    lambda f: self.draft(f, obligations[f.obligation_id], register, answers), todo
                )
            )
        by_id = {f.obligation_id: f for f in drafted}
        return [by_id.get(f.obligation_id, f) for f in base]
