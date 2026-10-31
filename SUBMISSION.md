# Submission notes

**Status:** working prototype.

**Live demo:** https://directive-impact-checker.streamlit.app/
**Code:** https://github.com/zayyankece/ad-checker

**Summary (for the form):**
Directive Impact Checker tells a continuing-airworthiness engineer which new FAA proposed airworthiness directives affect which aircraft in their fleet, what work they would require and by when, so they don't have to read every rule themselves. It indexes the full Federal Register text (1,341 pages). Two independent readers (rule-based and Claude) turn each directive's applicability into a rule, validated against the exact source sentence; a deterministic engine then decides per aircraft: Affects / Not affected / Need info. When the text can't decide, or the two readers disagree, it asks the engineer one question and saves the answer. It tracks revisions and withdrawals (and shows which aircraft moved in or out), estimates work-hours, drafts a cited comment to the FAA, and keeps a signed-off audit trail. Evaluated on 39 questions against plain Claude and naive RAG (hand-graded), plus a manual rule check; known failures are listed in the README.

**Run:**
```
pip install -r requirements.txt
make build && make fetch && make build
export ANTHROPIC_API_KEY=...      # optional; without it everything still runs (rule-based reader, extractive answers)
make extract && make test && make eval
make app
```

**Likely judge questions**
- *Isn't this just regex?* No single reader decides. A rule-based reader and an AI reader extract independently; both are checked word-for-word against the source; an omission check flags any model/MSN they drop; disagreement or uncertainty becomes a question for the engineer. The decision itself is deterministic code, as the rubric asks ("LLMs for reading, rules for compliance").
- *Where is the AI?* Reading applicability text (AI reader), answering free-text questions with verified citations (RAG + citation verifier), drafting FAA comments, explaining. Not in the yes/no applicability decision.
- *What fails?* See README "Known failures": confident-but-incomplete extraction (mitigated, not eliminated), applicability defined in external EASA ADs (asks the engineer), tables in old documents, a golden set written by the developer.
- *Why proposals (NPRMs)?* That is what the dataset contains. The value is early warning: plan hangar work and comment before the deadline.
