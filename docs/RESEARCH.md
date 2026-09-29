# What we learned from open-source GRC tools

Studied in September 2026 to shape the controls, evidence and privacy-operations modules.
Only ideas and data models were taken; no code was copied.

| Project | Licence | What it is | Where we looked |
|---|---|---|---|
| [Probo](https://github.com/getprobo/probo) | MIT | Go + GraphQL compliance platform (SOC 2, ISO 27001, GDPR) | `pkg/coredata` entities |
| [Openlane](https://github.com/theopenlane/core) | Apache 2.0 | Go compliance backend with ent schemas and workflows | `internal/ent/schema` |
| [CISO Assistant](https://github.com/intuitem/ciso-assistant-community) | AGPL 3.0 | Django GRC suite with a privacy module | `backend/privacy/models.py` |
| [Microsoft Presidio](https://github.com/microsoft/presidio) | MIT | PII detection engine | used directly by the discovery scanner |

Unicis and Conformiti were also suggested. We couldn't find a public repository for either
under the names we tried, so they aren't covered here.

## What we adopted

- **Records with a status workflow and a full history.** Probo's tasks
  (`BACKLOG → TODO → IN_PROGRESS → DONE / CANCELED`), rights requests
  (`TODO → IN_PROGRESS → DONE / REJECTED`) and document versions
  (`DRAFT → PENDING_APPROVAL → PUBLISHED`), and CISO Assistant's breaches
  (`discovered → under investigation → authority notified → subjects notified → closed`)
  all follow the same shape. We built one register engine (`web/registers.py`) that gives
  every register the same list, form, status workflow, history, comments and evidence.
- **Statuses that need facts.** A breach can't be "Board report sent" without the time it
  was sent; a request can't close without a written resolution; a policy can't be
  published without an approver. (Probo keeps "action taken" on requests; CISO Assistant
  keeps notification timestamps on breaches.)
- **Evidence linked to what it proves.** Probo links evidence to a measure and a task, and
  marks it requested or fulfilled. Ours links each file to an obligation and optionally a
  record, keeps versions (a new upload supersedes the old one), and counts it on the
  Controls page.
- **Control status separate from assessment findings.** Probo's obligations carry
  `NON_COMPLIANT / PARTIALLY_COMPLIANT / COMPLIANT`; Openlane separates controls from
  their implementation. Our Controls page records the client's own status
  (not started → implemented, or not applicable with a reason and an admin) next to what
  the assessment found.
- **Vendors with contract and location.** Probo's third parties carry DPA links, countries
  and a vetting status; ours carry the Section 8(2) contract, where data is processed
  (Section 16) and a review date.
- **DPIA fields.** Probo's DPIA: description, necessity and proportionality, potential
  risk, mitigations, residual risk. Ours uses the same, for Section 10(2)(c).

## What we did differently, for DPDPA

- Request types follow the Act: access (s.11), correction and erasure (s.12), grievance
  (s.13), nomination (s.14), withdrawal of consent (s.6(4)). The response clock defaults
  to 90 days, the Rule 14(3) maximum.
- The breach clock starts at awareness and puts the Board's detailed report 72 hours later
  (Rule 7(2)(b)); affected people are told without delay (Rule 7(1)).
- No "special category" or "sensitive personal data": the DPDP Act has none. Children's
  data (Section 9) is the one category with extra duties.
- Consent records are pseudonymous by design; the register is for proof, not a copy of
  the client's customer database.

## Not adopted (yet)

- Multi-organisation tenancy and SSO (Probo, Openlane). This app is built for a
  consultancy working on client engagements; a self-serve SaaS needs that decision first.
- Trust centres, cookie banners and access reviews (Probo): outside DPDPA's core.
- Workflow engines and approvals quorums (Openlane): a single approver is enough for now.
