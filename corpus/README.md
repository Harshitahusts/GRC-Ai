# DPDPA corpus

The authoritative texts the agent cites (tickets S1, H2 and H3 in the build plan).

## Add the sources (S1)

1. Download from **meity.gov.in only**: the DPDP Act 2023 and the DPDP Rules 2025, as PDFs.
   Don't use law-firm summaries or blogs as sources.
2. Put the PDFs in this folder.
3. Copy `manifest.example.json` to `manifest.json` and fill in one entry per file:
   `file`, `kind` (`act` or `rules`), `title`, `source_url` and `downloaded` (the date).

## Build it (H2)

```bash
grc-corpus ingest
```

This writes `build/chunks.jsonl`, `build/corpus_index.txt` and `build/report.json`. The
command prints the sections and rules it found and **any numbering gaps**. A gap usually
means a page didn't extract cleanly: open the PDF, check that page, and fix it before
relying on the corpus. The web app and the citation checker use `build/` automatically
once it exists.

## Check retrieval (H3)

```bash
grc-corpus search "who must be told about a data breach"
grc-corpus show "Section 8(6)"
grc-corpus eval questions.draft.json
```

The bar is 9 of 10 questions returning the right provision in the top 3.
`questions.draft.json` is a **draft**: check every expected provision against the
gazetted text before trusting the score, then rename it `questions.json`.

MeitY notifications and clarifications aren't parsed yet. Only the Act and the Rules are.
