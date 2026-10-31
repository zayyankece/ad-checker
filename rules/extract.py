"""APP-1: applicability text -> Rule JSON. LLM extractor with a regex fallback (used when no API key).

python -m rules.extract            # Airbus docs, LLM if ANTHROPIC_API_KEY is set, else regex
python -m rules.extract --regex    # force regex
"""
import argparse
import json
import os
import re

from adc.config import MODEL_EXTRACT
from adc.db import connect, dumps, rows

from .schema import Rule
from .validate import validate

SYSTEM = """You convert the Applicability paragraph of an FAA airworthiness directive into a JSON rule.
Hard rules:
- Use ONLY the text given. Never use outside knowledge about aircraft, serial numbers or other directives.
- Every condition needs a `quote` copied EXACTLY (character for character) from the text, covering the words that justify it.
- Every value you output must appear inside its quote. For abbreviated model lists like "A320-211, -212" output full names ("A320-211", "A320-212").
- "all Model X airplanes" -> scope "all", field "model". "X series airplanes" with no variant list -> field "model_family" with value "X".
- Serial number ranges -> field "msn", op "range", min/max (inclusive). Explicit MSN lists -> op "in". MSNs excluded -> put in `exclusions` with op "in".
- "except those with modification N" / "on which modification N has been embodied" -> exclusions, field "mods_installed", op "contains".
- "having part number P" / "fitted with P/N" -> field "parts_installed", op "contains".
- If applicability is defined by another document (e.g. "as identified in EASA AD 2026-0123", "listed in the effectivity of Service Bulletin X") -> add a condition field "external_document", op "requires_external", values ["EASA AD 2026-0123"].
- Separate OR alternatives into separate groups. Conditions in one group are ANDed.
- Anything you cannot map to these fields goes into `unresolved` as an exact quote. Do not guess.
- If the text has no applicability statement, return scope "unknown", groups [], and put the reason in `unresolved`."""

TOOL = {"name": "emit_rule", "description": "Return the applicability rule.", "input_schema": Rule.model_json_schema()}

TOKEN = re.compile(r"\b(A\d{3}|BD-\d{3}|EC\d{3}|AS\d{3}|H\d{3}|MBB-BK\d{3})(-[A-Z0-9]{1,6})?\b|(?<=[\s,])(-\d{1,3}[A-Z0-9]*)\b")
EXTERNAL = re.compile(r"(?:as identified in|identified in|listed in)\s+((?:European Union Aviation Safety Agency \(EASA\)|EASA|Transport Canada|TCCA)\s+AD\s+[A-Z]*-?\d{4}-\d{2,4}(?:R\d)?)", re.I)


def applicability_text(con, doc_id):
    ch = rows(con, "SELECT chunk_id, text FROM chunks WHERE doc_id=? AND section LIKE '%Applicability%' ORDER BY ord", (doc_id,))
    if ch:
        return ch[0]["chunk_id"], "\n\n".join(c["text"] for c in ch)
    s = rows(con, "SELECT chunk_id, text FROM chunks WHERE doc_id=? AND section='Summary (dataset)'", (doc_id,))
    return (s[0]["chunk_id"], s[0]["text"]) if s else (None, "")


def regex_extract(doc_id, text):
    sent_end = re.search(r"certificated in any category|airplanes\.|helicopters\.|rotorcraft\.", text)
    first = text[: sent_end.end()] if sent_end else text[:400]
    first = first.strip()
    models, fam, fams = [], None, []
    for m in TOKEN.finditer(first):
        full, f, var, abbr = m.group(0), m.group(1), m.group(2), m.group(3)
        if f:
            fam = f
            fams.append(f)
            if var:
                models.append(f + var)
        elif abbr and fam:
            models.append(fam + abbr)
    models = list(dict.fromkeys(models))
    groups, unresolved = [], []
    scope = "all" if re.search(r"\ball\b", first, re.I) else "certain"
    q = first if len(first) < 600 else first[:600]
    if models:
        groups.append({"conditions": [{"field": "model", "op": "in", "values": models, "quote": q}]})
    elif fams:
        groups.append({"conditions": [{"field": "model_family", "op": "in", "values": list(dict.fromkeys(fams)), "quote": q}]})
    ext = EXTERNAL.search(text)
    if ext and groups:
        groups[0]["conditions"].append({"field": "external_document", "op": "requires_external", "values": [ext.group(1)], "quote": ext.group(0)})
    elif scope == "certain":
        unresolved.append(first[:300])
    if not groups:
        scope = "unknown"
        unresolved.append("no model found in applicability text")
    return Rule(doc_id=doc_id, scope=scope, groups=groups, unresolved=unresolved)


def llm_extract(client, doc_id, text, meta):
    msg = client.messages.create(
        model=MODEL_EXTRACT, max_tokens=4000, system=SYSTEM,
        tools=[TOOL], tool_choice={"type": "tool", "name": "emit_rule"},
        messages=[{"role": "user", "content": f"doc_id: {doc_id}\nsubject: {meta['subject']}\n\nAPPLICABILITY TEXT:\n<<<\n{text}\n>>>"}],
    )
    data = next(b.input for b in msg.content if b.type == "tool_use")
    data["doc_id"] = doc_id
    return Rule.model_validate(data)


def extract_doc(con, doc_id, client=None):
    meta = rows(con, "SELECT * FROM documents WHERE doc_id=?", (doc_id,))[0]
    _, text = applicability_text(con, doc_id)
    errs, how = [], "regex"
    try:
        if client:
            rule, how = llm_extract(client, doc_id, text, meta), "llm"
        else:
            rule = regex_extract(doc_id, text)
    except Exception as e:
        rule = Rule(doc_id=doc_id, scope="unknown", groups=[], unresolved=[f"extraction failed: {e}"])
    errs = validate(rule, text)
    status = "needs_review" if errs or not rule.groups else ("partial" if rule.unresolved else "auto_validated")
    con.execute("INSERT OR REPLACE INTO rules VALUES (?,?,?,?,?,NULL,NULL)",
                (doc_id, rule.model_dump_json(), status, dumps(errs), how))
    con.commit()
    return rule, status, errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--regex", action="store_true")
    ap.add_argument("--doc")
    a = ap.parse_args()
    client = None
    if not a.regex and os.getenv("ANTHROPIC_API_KEY"):
        import anthropic
        client = anthropic.Anthropic()
    con = connect()
    ids = [a.doc] if a.doc else [r["doc_id"] for r in rows(con, "SELECT doc_id FROM documents WHERE is_airbus=1 AND doc_type!='WITHDRAWAL'")]
    stats = {}
    for i, d in enumerate(ids, 1):
        _, st, errs = extract_doc(con, d, client)
        stats[st] = stats.get(st, 0) + 1
        print(f"[{i}/{len(ids)}] {d} {st} {errs[:2] if errs else ''}")
    print("extractor:", "llm" if client else "regex", stats)


if __name__ == "__main__":
    main()
