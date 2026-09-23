# DPDPA agent KPIs

How we measure the v1 pilot, from Step 7 of the 30-day build plan. The scorecard is
computed by `grc-kpis` from one JSON record per engagement.

```bash
grc-kpis engagements/ --corpus-index corpus_index.txt
grc-kpis engagements/ --corpus-index corpus_index.txt --json
```

Try it on the fictional sample data:

```bash
grc-kpis examples/kpis/engagements --corpus-index examples/kpis/corpus_index.sample.txt
```

## North Star: Verified Engagements Delivered

The number of agent-assisted engagements that meet **all** of these:

1. The engagement is completed (delivered to the client).
2. It has at least one finding, and every finding carries at least one citation.
3. Every citation resolves against the corpus index.
4. It has at least one document, and every document passed human review.
5. The consultant did not fall back to manual drafting: the engagement isn't flagged
   `fell_back_to_manual` and no document was a `full_rewrite`.

For every engagement that doesn't count, the scorecard lists what's blocking it.

## Pilot KPIs

Only `mode: "agent"` engagements count. Findings, citations and documents from
engagements still in progress are included, so a fabricated citation shows up as soon as
it's recorded rather than after delivery.

| KPI | Target | How it's computed |
|---|---|---|
| Fabricated citations | 0 absolute | Count of citations, across all findings, that don't resolve. One occurrence pauses the project. |
| Citation resolution rate | 100% | Resolving citations / all citations. |
| Obligation coverage | 100% of register | Per engagement: distinct `obligation_id`s with a finding / `register_size`. The scorecard reports the lowest engagement. |
| Findings judged correct | 90% or better | `correct` / findings with a verdict. Unscored findings are reported, not counted. |
| Consultant hours per engagement | Under 6 | Mean `consultant_hours` of completed agent engagements. Compared to the manual baseline. |
| Documents needing full rewrite | Under 10% | `full_rewrite` / reviewed documents. |
| Intake completion without help | 70% or better | Share of engagements with `intake_completed_unaided: true`. |
| Intake submit to draft pack | Under 20 minutes | Mean minutes from `intake_submitted_at` to `draft_pack_ready_at`. |

**Baseline.** Consultant hours only mean something against the manual process. Record the
next manual engagements with `mode: "manual"` and their `consultant_hours`. The scorecard
warns until at least one exists.

**Metrics we deliberately don't track:** tokens processed, documents generated, time in
tool. They measure activity, not value.

## Dictionary

> **DRAFT: to be agreed by Harshit and Suryansh before Week 4 accuracy testing.** Change
> this section by pull request so the history shows what was agreed and when.

**Finding.** One assessed conclusion about one obligation in the register, for one
engagement. Its `status` is one of:
- `gap`: the client does not meet the obligation.
- `compliant`: the client meets it, based on intake answers and evidence.
- `open_item`: can't be decided. The client skipped the question, answered "not sure",
  or the evidence is missing. An open item is never a silent pass.
- `not_applicable`: the obligation doesn't apply (for example, no children's data), with
  the reason cited.

Every obligation in the register gets exactly one finding per engagement, including
`compliant` and `not_applicable` ones. That is what makes coverage measurable.

**Citation.** A reference to a provision in the corpus, written as `Section 5(1)`,
`Section 8(6)(a)`, `Rule 3(b)` or `Schedule`. Case, spacing, and the abbreviations `Sec.`,
`S.` and `R.` are normalized. Anything else, such as "Section 5(1) of the Act" or "DPDP
Rules r.3", can't be parsed and counts as unresolved.

**Resolved citation.** A citation whose provision appears in the corpus index, produced
from the authoritative MeitY texts. A parent resolves if any child is indexed (`Section 5`
resolves if `Section 5(1)` exists). A child never resolves from its parent alone. The
check is a deterministic string match, never a model call.

**Fabricated citation.** A citation that doesn't resolve. For KPI purposes every
unresolved citation counts as fabricated. The verifier already tolerates formatting, so an
unresolved citation is either invented or unusable, and both block the finding.

**Hallucination.** Any statement in a finding or document that isn't supported by the
provision it cites or by the client's intake answers and evidence. This includes a
fabricated citation, a real provision cited for something it doesn't say, or an invented
client fact. The reviewer logs it with `hallucination: true` on the finding. A
hallucinated finding is always scored `wrong`.

**Verdict** (accuracy scoring, S15). The consultant's judgement of a finding against the
expected answer:
- `correct`: right conclusion, right provision, right severity.
- `incomplete`: right direction but missing something material (a sub-obligation,
  evidence, or remediation step).
- `wrong`: wrong conclusion, wrong provision, or wrong severity.

**Human review.** A document counts as reviewed only when it has `reviewed_by`,
`reviewed_at` and an `outcome`. The review gate (H10) records these, and there is no
bypass.

**Material edit.** An edit that changes substance: a finding's conclusion, severity or
citation, a legal obligation or right stated in a document, or a party, purpose, data
category or retention period. Changes to wording, tone, formatting or order are not
material. Document `outcome` is one of:
- `usable`: sent with no edits, or wording-only edits.
- `minor_edits`: wording and small factual fixes, no material edits.
- `material_edit`: at least one material edit, but the structure and most content kept.
- `full_rewrite`: most of the content replaced. This counts as a fallback.

**Fallback.** The consultant abandoned the tool's output and produced a deliverable by
hand. Either a document outcome of `full_rewrite`, or `fell_back_to_manual: true` on
the engagement (for example, the gap report was redone manually).

**Consultant hours.** Total consultant time on the engagement from the first client call
to delivery, including review. Client time is excluded.

**Intake completed without help.** The client contact finished the intake form without
a consultant walking them through questions. A clarification call after the first agent
pass doesn't count as help.

## Engagement record format

One file per engagement in the engagements directory, for example `ENG-004.json`.
Timestamps are ISO 8601 with a timezone, such as `2026-10-19T10:00:00+05:30`.

```json
{
  "id": "ENG-004",
  "client": "Client name",
  "mode": "agent",
  "completed": true,
  "consultant_hours": 5.5,
  "register_size": 72,
  "intake_completed_unaided": true,
  "intake_submitted_at": "2026-10-19T10:00:00+05:30",
  "draft_pack_ready_at": "2026-10-19T10:16:00+05:30",
  "fell_back_to_manual": false,
  "findings": [
    {
      "id": "F-1",
      "obligation_id": "OBL-001",
      "status": "gap",
      "citations": ["Section 5(1)"],
      "verdict": "correct",
      "hallucination": false
    }
  ],
  "documents": [
    {
      "type": "ropa",
      "outcome": "minor_edits",
      "reviewed_by": "Harshit",
      "reviewed_at": "2026-10-19T12:00:00+05:30"
    }
  ]
}
```

Required: `id`, `client`, `mode` (`agent` or `manual`) and `completed`. Everything else is
optional and can be added as the engagement progresses. A manual baseline record needs
only the required fields plus `consultant_hours`.

Engagement records hold client information. Keep real ones out of the repository. The
`.gitignore` excludes a top-level `engagements/` folder, so keep them there.
