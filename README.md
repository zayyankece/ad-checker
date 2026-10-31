# Directive Impact Checker

Traceable search and fleet-impact checker for proposed FAA Airworthiness Directives.
Autonomous SG 2026, Airbus Track, Challenge #4.

> Status of this file: **requirements spec**. Every feature built must map to a requirement ID below, and every requirement has a pass/fail check. Tick the box only when the check passes.

## Quick start

```bash
pip install -r requirements.txt
make build                 # load xlsx → chunks → FTS index → revision graph (offline)
make fetch                 # Federal Register full text for Airbus docs (needs internet)
make build                 # re-chunk with full text
export ANTHROPIC_API_KEY=...
make extract               # applicability rules (LLM; regex fallback without key)
make test && make eval
make app                   # Streamlit UI
```

Order matters: `fetch` and `extract` read the database built by `build`. Re-run `build` after `fetch`, then `extract`.

Actual code layout: `adc/` (config, db, fleet), `ingest/` (load, fetch, chunk, revisions), `rules/` (schema, extract, validate, engine), `qa/` (retrieve, verify, pipeline), `app/`, `eval/`, `tests/`.

## Current results (update after every eval run)

After Federal Register full-text fetch (67 Airbus documents), regex extractor, extractive answers (no API key):

| Metric | Value |
|---|---|
| Golden set | 39/39 pass (lookup 7, applicability 14, revision 5, out of scope 6, ambiguous 3, false premise 4) |
| Refusal precision / recall | 1.0 / 1.0 |
| Retrieval latency p95 | ~6 ms over 1,341 pages (2,816 chunks) |
| Rule engine unit tests | 15/15 |
| Airbus rules | 36 auto-validated, 12 partial (unresolved text → CANNOT DETERMINE), 27 needs review |

Caveat: the golden set was written by the same developer who built the system, so these numbers overstate real-world accuracy. Baselines (B0/B1) need an API key: `python -m eval.run --baselines`.

**Findings that show why full text + rules matter**
- `FAA-2026-11813`: the dataset summary lists A330-243/-243F/-841/-941 (scope of the AD being replaced). The proposal's own section (c) narrows it to A330-941 only, "as identified in EASA AD 2025-0286". A summary-based system would wrongly flag the A330-243 (golden A5, P4).
- `FAA-2026-1338`: the revised proposal (2026-10-05) moves the certificate-date cutoff from 1 Oct 2024 to 19 Dec 2025. 9V-ZMF (A330-941, certificated 2025-03-10) goes from NOT APPLICABLE to APPLIES. Shown automatically in the version diff (golden V5, A13).

**Known failures (so far)**
1. `2002-NM-09-AD` says "All Model A330 series airplanes" (2003). The engine matches the A330-941 (A330neo, which did not exist then) by family. Literal reading is correct; engineering intent is doubtful. Should be flagged for review, currently is not.
2. Regex extractor cannot read applicability tables (e.g. `2002-NM-224-AD`, MSN ranges with exception lists). Marked `needs_review`, so the result is CANNOT DETERMINE, never a wrong APPLIES/NOT APPLICABLE. LLM extractor is expected to fix this.
3. Modern Airbus proposals often define applicability as "as identified in EASA AD …". That document is not in the dataset, so those aircraft stay CANNOT DETERMINE by design.
4. Without full text, 2026 documents only have the dataset summary ("certain … airplanes"), so most results are CANNOT DETERMINE.

---

## 0. Glossary

| Term | Meaning |
|---|---|
| Airworthiness Directive (AD) | Legally binding order from the regulator (here: FAA, the US Federal Aviation Administration) requiring an inspection or fix on specific aircraft, engines or parts |
| NPRM | Notice of Proposed Rulemaking. A **proposed** directive, open for public comment, **not yet binding**. The whole dataset is NPRMs |
| SNPRM | Supplemental NPRM. A revised proposal under the same docket |
| Docket | FAA case file ID (e.g. `FAA-2026-11813`). One docket can have several documents over time |
| Supersede | A new directive replaces an older one (e.g. "proposes to supersede AD 2025-09-07") |
| MSN | Manufacturer Serial Number, unique per aircraft |
| Applicability | The section of a directive that says exactly which aircraft/parts it covers |
| Continuing airworthiness engineer | Engineer at an airline responsible for tracking which directives apply to which aircraft |
| Federal Register | Official US government journal where the full text of every NPRM is published |

---

## 1. Problem and user

**User:** Continuing airworthiness engineer at a fictional Airbus operator ("Merlion Air", 8 aircraft: A320 family, A330, A350, A220).

**Weekly task:** New proposed directives are published. For each one the engineer must:
1. Decide which aircraft in the fleet it would affect.
2. Understand what work it would require.
3. Check whether it replaces or revises an earlier document.
4. Decide whether to submit a comment before the comment deadline.

**Status quo:** Read each directive manually, compare the applicability text against fleet records, enter the result into the maintenance system by hand. Failure modes: misreading serial ranges, mixing up model variants (A330-941 vs A330-943), missing that a proposal was revised or withdrawn.

**Why generic AI fails here:** LLMs blend instructions across aircraft types and fill gaps from memory. One wrong "does not apply" is a safety issue.

**Design principle:** LLM reads and explains. Code decides. A human approves.

---

## 2. Scope (MVP, deadline today)

**In scope**
- Index full text of all 431 rows (~1,341 pages) for search.
- Deep applicability parsing and fleet checking for **Airbus aircraft only** (82 rows, 71 dockets, ~299 pages).
- Fleet database loaded once from a CSV.
- Natural language Q&A with citations.
- Per-aircraft impact report for any directive.
- Revision history per docket and supersede links.
- Evaluation script with baselines.

**Out of scope**
- Non-Airbus applicability parsing (searchable only).
- Engines, propellers, appliances as fleet items beyond engine model.
- Login, notifications, PDF upload of arbitrary documents, dashboards.

---

## 3. Data

**Source file:** `AD_Notice_of_Proposed_Rulemaking_AD_NPRM.xlsx`, 431 rows, 22 columns.

Known facts (verified from the file):

| Fact | Consequence |
|---|---|
| Rows are NPRMs (proposals), not final directives | Every answer must state "proposed, not yet binding" |
| `Summary` is ~800 characters, no serial ranges (327/431 say only "certain airplanes") | Must download full text |
| 406/431 rows link to Federal Register; all 82 Airbus rows have a link | Full text available via Federal Register API |
| 431 rows, 323 unique dockets | Multiple versions per docket → revision logic |
| 100 summaries contain "supersede" (29 Airbus) | Cross-directive links |
| `Status` is "Current" on every row, including withdrawals | Do **not** trust `Status`; derive it |
| `Make` inconsistent ("Airbus", "Airbus SAS", "Airbus Industrie", …) | Alias table |
| `Model` empty on 345/431 rows | Extract models from text |
| `Comments` holds withdrawal / correction / deadline-extension notes (5 rows) | Input to revision logic |
| 21 rows have `AD Number` filled | Open question: likely final rule adopted. Verify |

**Full text retrieval** (run on laptop, not in sandbox):
- Document number from link: `/documents/YYYY/MM/DD/<doc_number>/...`
- Metadata: `https://www.federalregister.gov/api/v1/documents/<doc_number>.json`
- Use `full_text_xml_url` (structured sections). Fallback: `raw_text_url`.
- Cache raw files in `data/raw/` so the demo runs offline.

---

## 4. Architecture

```mermaid
flowchart TD
  subgraph Offline ingest
    X[xlsx] --> N[Normalize metadata<br/>alias table, doc numbers]
    N --> F[Fetch full text<br/>Federal Register API]
    F --> C[Chunk by section<br/>keep page + section ID]
    C --> IDX[(SQLite + FTS5 index)]
    N --> REV[Revision graph<br/>rules only]
    C --> EXT[Applicability extraction<br/>LLM → JSON rule]
    EXT --> VAL{Every value appears<br/>verbatim in source?}
    VAL -- yes --> R1[(Rule: auto_validated)]
    VAL -- no --> R2[(Rule: needs_review)]
    R1 & R2 --> HR[Engineer approves rule in UI]
  end

  subgraph Fleet
    FL[fleet.csv] --> FDB[(Fleet table)]
  end

  subgraph Query time
    Q[User question] --> QP[Query parser<br/>LLM → JSON intent]
    QP --> ENT{Entities exist<br/>in data?}
    ENT -- no --> REF[Refuse: out of scope]
    ENT -- yes --> RET[Metadata filter + BM25]
    RET --> ENG[Rule engine<br/>3-valued, deterministic]
    FDB --> ENG
    HR --> ENG
    ENG --> GEN[Answer generator<br/>LLM, cites chunk IDs]
    RET --> GEN
    GEN --> VER{Citation verifier<br/>quotes match source?}
    VER -- pass --> OUT[Structured answer]
    VER -- fail --> REF2[Refuse: cannot verify]
  end
```

**Rule:** the LLM never decides applicability. It only (a) turns applicability text into a rule, (b) parses the question, (c) phrases the answer from verified chunks.

---

## 5. Data model

**documents**: `doc_number` (PK), `docket`, `issue_date`, `publish_date`, `comments_due`, `make_raw`, `make_norm`, `subject`, `doc_type` (NPRM / SNPRM / WITHDRAWAL / CORRECTION), `fr_citation`, `fr_pages`, `url`, `supersedes_ad` (nullable), `summary`

**chunks**: `chunk_id` (PK, e.g. `2026-21334#c`), `doc_number`, `section` (e.g. `(c) Applicability`), `page`, `text`

**revisions**: `docket`, `doc_number`, `version_index`, `is_latest`, `docket_status` (ACTIVE_PROPOSAL / WITHDRAWN / ADOPTED_UNVERIFIED)

**rules**: `doc_number`, `rule_json`, `validation` (auto_validated / needs_review), `approved_by`, `approved_at`

**fleet**: `registration`, `model`, `msn`, `engine_model`, `mods_installed` (list), `parts_installed` (list of part number + serial)

**Rule JSON** (OR of groups, each group AND of conditions, then exclusions):
```json
{
  "doc_number": "2026-21334",
  "scope": "certain",
  "groups": [
    {"conditions": [
      {"field": "model", "op": "in", "values": ["A330-243", "A330-941"], "quote": "Model A330-243 and -941 airplanes"},
      {"field": "msn", "op": "range", "min": 1000, "max": 1500, "quote": "manufacturer serial numbers 1000 through 1500"}
    ]}
  ],
  "exclusions": [
    {"field": "mods_installed", "op": "contains", "value": "12345", "quote": "except those on which modification 12345 has been embodied"}
  ],
  "unresolved": ["text the extractor could not map to a field"]
}
```

**Evaluation logic (three-valued):** each condition → TRUE / FALSE / UNKNOWN (UNKNOWN when the fleet field is missing). AND/OR follow standard three-valued logic. Any `unresolved` text → result can be at most UNKNOWN.

| Result | Shown as |
|---|---|
| TRUE | **APPLIES** |
| FALSE | **NOT APPLICABLE** |
| UNKNOWN | **CANNOT DETERMINE** + list of missing fields / unresolved text |

---

## 6. Requirements

Priority: **P0** = must ship for demo, **P1** = ship if time, **P2** = only if everything else passes.

### Ingest and indexing

| ID | Pri | Requirement | Pass check | Done |
|---|---|---|---|---|
| ING-1 | P0 | Load xlsx, extract `doc_number` from link, normalize `Make` via alias table | All 82 Airbus rows have `make_norm` in {Airbus, Airbus Helicopters, Airbus Canada, Airbus Defence and Space} and a `doc_number` | [ ] |
| ING-2 | P0 | Fetch and cache full text for all rows with a link | ≥ 95% of 406 linked rows cached in `data/raw/`; failures listed in `data/fetch_errors.csv` | [ ] |
| ING-3 | P0 | Chunk by section; each chunk keeps doc number, section label, page | For 5 random Airbus docs, `(c) Applicability` is its own chunk and the text matches the Federal Register page | [ ] |
| ING-4 | P0 | Full-text index (SQLite FTS5, BM25) over all chunks | Index covers all cached docs; query returns in < 3 s (see EVAL-5) | [ ] |
| ING-5 | P1 | Add embeddings for hybrid retrieval | Recall@5 on golden set improves vs BM25 only, or is dropped | [ ] |

### Revision logic

| ID | Pri | Requirement | Pass check | Done |
|---|---|---|---|---|
| REV-1 | P0 | Group by docket, order by date, mark latest | Every docket with > 1 row has exactly one `is_latest` | [ ] |
| REV-2 | P0 | Derive `doc_type` from subject/summary/comments ("Withdrawal", "supplemental", "OFR correction") | All withdrawal rows in Airbus subset (≥ 3) labelled WITHDRAWAL | [ ] |
| REV-3 | P0 | Extract supersede target (`AD YYYY-NN-NN`) | All 29 Airbus "supersede" rows have `supersedes_ad` filled | [ ] |
| REV-4 | P0 | Any answer citing a non-latest or withdrawn document shows a warning with the latest version | Golden set revision questions: 100% show the warning | [ ] |
| REV-5 | P1 | Diff between versions of the same docket: changed applicability and changed actions, plus fleet impact delta | For one multi-version Airbus docket, diff lists changed sections and which aircraft entered/left scope | [ ] |

### Applicability rules

| ID | Pri | Requirement | Pass check | Done |
|---|---|---|---|---|
| APP-1 | P0 | LLM extracts rule JSON from the Applicability chunk, using the schema in §5 | Valid JSON for 100% of Airbus docs (schema-validated) | [ ] |
| APP-2 | P0 | Verbatim validator: every `quote` must be a substring of the source chunk (whitespace/case normalized); every value must appear inside its quote | Failing rules are marked `needs_review`, never `auto_validated` | [ ] |
| APP-3 | P0 | Deterministic three-valued rule engine | Unit tests pass for: in-list, range boundaries (min, max, min-1, max+1), missing field → UNKNOWN, exclusion, unresolved text → UNKNOWN | [ ] |
| APP-4 | P0 | Rule review screen: rule shown side by side with source quotes; engineer can approve or edit | Approved rules store `approved_by` + timestamp | [ ] |
| APP-5 | P0 | Extraction accuracy measured against hand labels | Hand-label ≥ 15 Airbus docs; report field-level precision/recall in §8 | [ ] |

### Fleet impact

| ID | Pri | Requirement | Pass check | Done |
|---|---|---|---|---|
| FLT-1 | P0 | Load `fleet.csv` (8 fictional aircraft) | Loaded with schema validation; bad rows rejected with reason | [ ] |
| FLT-2 | P0 | Directive → fleet report: each aircraft APPLIES / NOT APPLICABLE / CANNOT DETERMINE with the quote that decided it | Matches hand labels for all (aircraft × hand-labelled doc) pairs, or mismatches listed in §8 | [ ] |
| FLT-3 | P0 | CANNOT DETERMINE lists exactly which fleet field or condition is missing, phrased as a question | e.g. "Is modification 12345 embodied on MSN 1234? Not in fleet data." | [ ] |
| FLT-4 | P0 | Every report shows: proposed (not binding), comment due date, latest version status | Present on 100% of reports | [ ] |
| FLT-5 | P1 | Aircraft → directives: list all directives that apply / might apply to one aircraft | Works for every aircraft in fleet | [ ] |

### Q&A

| ID | Pri | Requirement | Pass check | Done |
|---|---|---|---|---|
| QA-1 | P0 | Query parser (LLM → JSON: intent, aircraft, model, topic, docket) | Entities checked against DB before retrieval | [ ] |
| QA-2 | P0 | Retrieval with metadata filter (make/model) then BM25 | Recall@5 reported (EVAL-2) | [ ] |
| QA-3 | P0 | Applicability questions about fleet aircraft are answered by the rule engine, not the LLM | Code path test: LLM output cannot change the APPLIES/NOT/CANNOT label | [ ] |
| QA-4 | P0 | Answer = list of claims, each with `chunk_id` + verbatim quote | Output schema-validated | [ ] |
| QA-5 | P0 | Citation verifier: every quote must exist in its cited chunk; one failure → regenerate once → refuse | Fault-injection test: tampered quote causes refusal | [ ] |
| QA-6 | P0 | Refusal policy (§7) implemented as explicit checks, not LLM judgement | Each refusal reason R1–R7 has at least one golden-set case that triggers it | [ ] |
| QA-7 | P0 | System prompt forbids using outside knowledge; answer only from provided chunks | Covered by out-of-scope and false-premise cases in EVAL-3 | [ ] |

### Interface

| ID | Pri | Requirement | Pass check | Done |
|---|---|---|---|---|
| UI-1 | P0 | Streamlit app, 3 tabs: Ask, Fleet impact, Rule review | All three usable end to end in demo script | [ ] |
| UI-2 | P0 | Every citation clickable → shows chunk text, section, page, Federal Register link | Click-through works on 5 random answers | [ ] |
| UI-3 | P0 | Status labels and refusals visually distinct (not hidden in prose) | Visible in demo | [ ] |
| UI-4 | P1 | "Add new directive" by docket or document number → fetch, parse, rule review, fleet report | Works live for one Airbus doc | [ ] |

---

## 7. Refusal policy

The system refuses or returns CANNOT DETERMINE when any of these checks fires. Each is code, not LLM judgement.

| Code | Trigger | Output |
|---|---|---|
| R1 | No chunk above BM25 score threshold for the filtered set | "No document in the dataset covers this." |
| R2 | Model, aircraft, docket or directive number not found in data | "X is not in the dataset." (never "X is safe" or "no directive applies") |
| R3 | Ambiguous entity (e.g. "A330" when directives differ by variant and fleet has several) | Ask which variant/aircraft |
| R4 | Rule needs a fleet field that is missing | CANNOT DETERMINE + missing field question |
| R5 | Citation verifier fails after one retry | "Could not produce a verifiable answer." + show retrieved chunks |
| R6 | Question premise contradicts data (e.g. "since directive X covers MSN 2000…" when it does not) | Point out the contradiction with citation |
| R7 | Question asks what is *required* but the latest document is a proposal or withdrawn | State status first; answer only as "proposed" |

---

## 8. Evaluation

**Golden set** (`eval/golden.jsonl`, ≥ 30 questions, written before tuning):

| Category | n | Example |
|---|---|---|
| Lookup | 6 | "What inspection does docket FAA-2026-11813 propose?" |
| Fleet applicability | 8 | "Does it affect 9V-MRA?" incl. serial boundary cases |
| Revision | 4 | Question about an older version / withdrawn proposal |
| Out of scope | 4 | Model not in data, Boeing deep applicability, "is it safe to fly" |
| Ambiguous | 4 | "Does the A330 directive apply?" |
| False premise | 4 | Wrong serial range or wrong model stated in question |

**Baselines**
- B0: Claude, no retrieval (pure memory).
- B1: Naive RAG: BM25 top-5 → Claude, no filters, no rule engine, no verifier.
- Ours.

| ID | Metric | Target |
|---|---|---|
| EVAL-1 | Answer correctness (hand-graded) | Ours > B1 > B0 |
| EVAL-2 | Retrieval recall@5 | Report |
| EVAL-3 | Refusal precision / recall on out-of-scope + ambiguous + false premise | Recall ≥ 0.9 |
| EVAL-4 | Applicability accuracy per (aircraft × doc) vs hand labels | Report, list every error |
| EVAL-5 | Latency p50 / p95: retrieval only, and end to end | Retrieval p95 < 3 s over full index |
| EVAL-6 | Citation verifier pass rate | Report |

**Results table:** fill in after `python eval/run.py`. Include a **Known failures** list with at least 3 real failure cases and why they happen. Do not hide them.

---

## 9. Traceability: challenge requirement → spec

| Challenge requirement | Requirement IDs | Evidence |
|---|---|---|
| Ingest multi-page documents | ING-2, ING-3 | 1,341 pages indexed |
| Parse exact applicability rules | APP-1, APP-2, APP-3 | Extraction accuracy (APP-5) |
| Natural language queries with citations | QA-1 – QA-5, UI-2 | Golden set |
| Every claim links to source metadata | QA-4, QA-5, UI-2 | Verifier pass rate |
| Zero hallucination | QA-5, QA-7, APP-2 | B0/B1 vs ours |
| Configuration filtering | APP-3, FLT-2, QA-3 | Boundary tests |
| Revision logic | REV-1 – REV-5 | Revision questions |
| Communicate uncertainty | §7, FLT-3 | Refusal metrics |
| Sub 3-second retrieval, 1000+ pages | ING-4, EVAL-5 | Latency table |
| Avoid LLM guessing | QA-3, QA-7 | B0 comparison |
| Structured output | QA-4, FLT-2 | Schema validation |

| Rubric criterion | Where it is addressed |
|---|---|
| Problem realism | §1 named user and workflow |
| LLM / non-LLM split | §4 principle, QA-3, APP-3 |
| Honest evaluation | §8 baselines + known failures |
| Trust and traceability | §7, APP-4 (human approves), UI-2 |
| Impact | Before/after time per directive (estimate, marked as estimate) |

---

## 10. Demo script (3 min)

1. (20 s) User + problem: one engineer, 8 aircraft, a new proposal every week.
2. (40 s) Fleet impact tab: pick an Airbus A330 proposal → 2 APPLIES, 5 NOT APPLICABLE, 1 CANNOT DETERMINE with the missing-data question. Click a quote → Federal Register page.
3. (30 s) Rule review tab: the rule next to its source text, engineer approves.
4. (40 s) Ask tab: normal question with citations → older-version question (revision warning) → out-of-scope question (refusal) → false-premise question (contradiction flagged).
5. (40 s) Evaluation table: B0 vs B1 vs ours, plus one known failure.
6. (10 s) Close: LLM reads, code decides, engineer approves.

---

## 11. Tech stack

- Python 3.11, pandas, `requests`, `lxml`
- SQLite + FTS5 (BM25)
- Claude API: Sonnet for applicability extraction (offline, once per doc); Haiku for query parsing; Sonnet for answer generation
- `pydantic` for every JSON schema
- Streamlit
- `pytest` for rule engine + verifier

## 12. Repo layout

```
data/        xlsx, raw/ (cached full text), fleet.csv, aliases.csv
ingest/      load.py, fetch.py, chunk.py, revisions.py
rules/       extract.py, validate.py, engine.py
qa/          parse.py, retrieve.py, answer.py, verify.py, refuse.py
app/         streamlit_app.py
eval/        golden.jsonl, labels/, run.py, results.md
tests/
```

## 13. Build order (cut from the bottom if late)

| # | Block | Requirements | Est. |
|---|---|---|---|
| 1 | Load, normalize, fetch, chunk, index | ING-1–4 | 1.5 h |
| 2 | Revision graph | REV-1–4 | 0.5 h |
| 3 | Extraction + validator + rule engine + tests | APP-1–3 | 1.5 h |
| 4 | Fleet CSV + impact report | FLT-1–4 | 0.5 h |
| 5 | Q&A + verifier + refusal checks | QA-1–7 | 1.5 h |
| 6 | Streamlit UI | UI-1–3, APP-4 | 1 h |
| 7 | Golden set, hand labels, eval run | APP-5, EVAL-1–6 | 1 h |
| 8 | P1 items | ING-5, REV-5, FLT-5, UI-4 | if time |

## 14. Open questions / risks

- Does `AD Number` filled mean the proposal was adopted as a final rule? Verify on 2–3 rows before relying on it.
- Federal Register XML structure for older documents (2003–2008) may differ; fallback to raw text + regex for "(c) Applicability".
- Applicability text that depends on modification or service bulletin status cannot be checked without that data → CANNOT DETERMINE by design.
- Fleet is fictional; MSNs chosen so that some fall inside, outside and on the boundary of real ranges.
