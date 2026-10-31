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
- "with an original airworthiness certificate ... issued on or before <date>" -> field "cert_date", op "on_or_before" (or "on_or_after"), values ["YYYY-MM-DD"].
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


CERT_DATE = re.compile(r"original (?:export )?(?:certificate of )?airworthiness certificate[^.;]*?issued (on or before|on or after) ([A-Z][a-z]+ \d{1,2}, \d{4})")
LIMITERS = re.compile(
    r"[^.;]*\b(on or before|on or after|serial numbers?|part numbers?|P/N|except|excluding|modification|"
    r"service bulletin|if |having|fitted with|equipped with|on which)\b[^.;]*[.;]?", re.I)


EXCEPT = re.compile(r"\b(except|excluding|other than|but not)\b", re.I)
MSN_RANGE = re.compile(r"(?:manufacturer(?:'s)? serial numbers?|MSNs?)\s*\(?(?:MSN)?\)?\s*(\d{2,6})\s+(?:through|to)\s+(\d{2,6})(?:\s+inclusive)?", re.I)
MSN_LIST = re.compile(r"(?:manufacturer(?:'s)? serial numbers?|MSNs?)\s+((?:\d{2,6}(?:\s*,\s*(?:and\s+)?|\s+and\s+|\s+or\s+)?)+)", re.I)
MOD = re.compile(r"modifications?\s+(?:No\.\s*)?(\d{3,}[A-Z0-9]*)", re.I)
NEUTRAL = re.compile(r",?\s*all (?:manufacturer(?:'s)? )?serial numbers", re.I)


def parse_models(s, fam=None):
    models, fams = [], []
    for m in TOKEN.finditer(s):
        f, var, abbr = m.group(1), m.group(2), m.group(3)
        if f:
            fam = f
            fams.append(f)
            if var:
                models.append(f + var)
        elif abbr and fam:
            models.append(fam + abbr)
    fam_set = list(dict.fromkeys(fams))
    if not models and len(fam_set) == 1:
        # spaced variant names, e.g. "A300 B4-601, B4-603"
        models = [f"{fam_set[0]} {v}" for v in re.findall(r"(?<![A-Z0-9-])([BCF]\d-\d{1,3}[A-Z]?)\b", s)]
    return list(dict.fromkeys(models)), fam_set, fam


def regex_extract(doc_id, text):
    """Deterministic extractor. Conservative: anything it cannot model becomes `unresolved` (-> Need info).
    Splits inclusion from exclusion ("all A320 except A320-251N") so an exception is never read as the scope."""
    body = re.sub(r"^\s*(?:\([a-z]\)\s*)?Applicability:?\s*", "", text.strip())
    body = re.split(r"\n\s*\(d\)\s+Subject|\n\s*Compliance:", body)[0].strip()
    if text.strip().startswith("The FAA") or not body:
        body = text.strip()
    scan = re.sub(r"\s+", " ", re.sub(r"-\s+(?=\w)", "-", body))
    groups, exclusions, unresolved = [], [], []

    ex = EXCEPT.search(scan)
    if ex:
        end = re.search(r"[.;]", scan[ex.start():])
        excl = scan[ex.start(): ex.start() + end.end()] if end else scan[ex.start():]
        incl = scan[:ex.start()] + " " + scan[ex.start() + len(excl):]
    else:
        excl, incl = "", scan

    models, fams, last_fam = parse_models(incl)
    scope = "all" if re.search(r"\bapplies to all\b|^all\b|\bAll Model\b", incl, re.I) else "certain"
    if models:
        groups.append({"conditions": [{"field": "model", "op": "in", "values": models, "quote": body}]})
    elif fams:
        groups.append({"conditions": [{"field": "model_family", "op": "in", "values": fams, "quote": body}]})

    handled = []
    ext = EXTERNAL.search(incl)
    if ext and groups:
        groups[0]["conditions"].append({"field": "external_document", "op": "requires_external", "values": [ext.group(1)], "quote": ext.group(0)})
        handled.append(ext.group(0))
    cd = CERT_DATE.search(incl)
    if cd and groups:
        from datetime import datetime
        iso = datetime.strptime(cd.group(2), "%B %d, %Y").date().isoformat()
        groups[0]["conditions"].append({"field": "cert_date", "op": cd.group(1).replace(" ", "_"), "values": [iso], "quote": cd.group(0)})
        handled.append(cd.group(0))
    rg = MSN_RANGE.search(incl)
    if rg and groups:
        groups[0]["conditions"].append({"field": "msn", "op": "range", "min": int(rg.group(1)), "max": int(rg.group(2)), "quote": rg.group(0)})
        handled.append(rg.group(0))

    if excl:
        found = False
        ex_models, ex_fams, _ = parse_models(excl, last_fam)
        if ex_models:
            from .validate import norm, value_supported
            q = excl if all(value_supported(v, norm(excl)) for v in ex_models) else body
            exclusions.append({"field": "model", "op": "in", "values": ex_models, "quote": q})
            found = True
        erg = MSN_RANGE.search(excl)
        if erg:
            exclusions.append({"field": "msn", "op": "range", "min": int(erg.group(1)), "max": int(erg.group(2)), "quote": excl})
            found = True
        else:
            el = MSN_LIST.search(excl)
            if el:
                exclusions.append({"field": "msn", "op": "in", "values": re.findall(r"\d{2,6}", el.group(1)), "quote": excl})
                found = True
        mods = MOD.findall(excl)
        if mods:
            exclusions.append({"field": "mods_installed", "op": "contains", "values": mods, "quote": excl})
            found = True
        if not found or re.search(r"\b(if|unless|having|fitted with|service bulletin)\b", excl, re.I):
            unresolved.append(excl.strip()[:300])

    rest = NEUTRAL.sub("", incl)
    for h in handled:
        rest = rest.replace(h, "")
    for lm in LIMITERS.finditer(rest):
        unresolved.append(lm.group(0).strip()[:300])
    if re.search(r"\bTable \d", scan):
        unresolved.append("applicability is given in a table")
    if scope == "certain" and not handled and not exclusions and not unresolved:
        unresolved.append(scan[:300])
    if not groups:
        scope = "unknown"
        unresolved.append("no model found in applicability text")
    return Rule(doc_id=doc_id, scope=scope, groups=groups, exclusions=exclusions, unresolved=list(dict.fromkeys(unresolved)))


def llm_extract(client, doc_id, text, meta):
    from adc.llm import call_tool
    data = call_tool(client, MODEL_EXTRACT, SYSTEM, TOOL,
                     f"doc_id: {doc_id}\nsubject: {meta['subject']}\n\nAPPLICABILITY TEXT:\n<<<\n{text}\n>>>")
    data["doc_id"] = doc_id
    return Rule.model_validate(data)


RANK = {"auto_validated": 0, "partial": 1, "needs_review": 2}


def _run(con, doc_id, text, meta, how, client):
    try:
        rule = llm_extract(client, doc_id, text, meta) if how == "llm" else regex_extract(doc_id, text)
    except Exception as e:
        rule = Rule(doc_id=doc_id, scope="unknown", groups=[], unresolved=[f"extraction failed: {e}"])
    errs = validate(rule, text)
    if not errs and rule.groups:
        from .validate import coverage_gaps
        gaps = coverage_gaps(rule, text)
        if gaps:
            rule.unresolved.append("Mentioned in the text but not used by the rule: " + ", ".join(gaps[:12]))
    status = "needs_review" if errs or not rule.groups else ("partial" if rule.unresolved else "auto_validated")
    return rule, status, errs


def extract_doc(con, doc_id, client=None, mode="dual"):
    """dual (default with an API key): regex and the LLM read the text independently. The better-validated rule is
    primary; the other is kept as a second reader. Where the two disagree for an aircraft, the result is Need info.
    Without an API key this is regex only."""
    meta = rows(con, "SELECT * FROM documents WHERE doc_id=?", (doc_id,))[0]
    _, text = applicability_text(con, doc_id)
    alt = alt_how = None
    if mode == "llm" and client:
        rule, status, errs = _run(con, doc_id, text, meta, "llm", client)
        how = "llm"
    else:
        rule, status, errs = _run(con, doc_id, text, meta, "regex", None)
        how = "regex"
        if mode in ("dual", "hybrid") and client and (mode == "dual" or status != "auto_validated"):
            r2, s2, e2 = _run(con, doc_id, text, meta, "llm", client)
            if RANK[s2] < RANK[status]:
                if status != "needs_review":
                    alt, alt_how = rule, "regex"
                rule, status, errs, how = r2, s2, e2, "llm"
            elif s2 != "needs_review":
                alt, alt_how = r2, "llm"
    con.execute("INSERT OR REPLACE INTO rules (doc_id, rule_json, validation, validation_errors, extractor, approved_by, approved_at, "
                "alt_rule_json, alt_extractor) VALUES (?,?,?,?,?,NULL,NULL,?,?)",
                (doc_id, rule.model_dump_json(), status, dumps(errs), how, alt.model_dump_json() if alt else None, alt_how))
    con.commit()
    return rule, status, errs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--regex", action="store_true", help="regex only")
    ap.add_argument("--llm", action="store_true", help="LLM only (for comparison)")
    ap.add_argument("--doc")
    a = ap.parse_args()
    client = None
    if not a.regex:
        from adc.llm import client as mk
        client = mk()
    con = connect()
    ids = [a.doc] if a.doc else [r["doc_id"] for r in rows(con, "SELECT doc_id FROM documents WHERE is_airbus=1 AND doc_type!='WITHDRAWAL'")]
    mode = "regex" if a.regex else ("llm" if a.llm else "dual")
    stats, used = {}, {}
    for i, d in enumerate(ids, 1):
        _, st, errs = extract_doc(con, d, client, mode)
        how, alt = con.execute("SELECT extractor, alt_extractor FROM rules WHERE doc_id=?", (d,)).fetchone()
        how = f"{how}+{alt}" if alt else how
        stats[st] = stats.get(st, 0) + 1
        used[how] = used.get(how, 0) + 1
        print(f"[{i}/{len(ids)}] {d} {st} via {how} {errs[:1] if errs else ''}")
    print(f"mode: {mode}{'' if client else ' (no API key: regex only)'} | status {stats} | extractor used {used}")


if __name__ == "__main__":
    main()
