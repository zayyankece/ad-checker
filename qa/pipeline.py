"""QA-1..7: question -> structured, cited answer or explicit refusal."""
import os
import re
import time

from adc.config import BM25_MIN_SCORE, MODEL_ANSWER
from adc.db import one, rows
from adc.fleet import aircraft_report, doc_status, get_rule, impact_report
from rules.engine import evaluate, family, norm_model

from .retrieve import search
from .verify import verify

DOCKET = re.compile(r"\b(FAA-\d{4}-\d{3,5}(?:R1)?|\d{4}-[A-Z]{2}-\d{2,3}-AD)\b", re.I)
DOCNUM = re.compile(r"\b(20\d{2}-\d{5})\b")
ADNUM = re.compile(r"\bAD\s+(\d{4}-\d{2}-\d{2})\b", re.I)
REG = re.compile(r"\b(9V-[A-Z]{3})\b", re.I)
MSN = re.compile(r"\bMSN\s*(\d{2,6})\b", re.I)
MSN_RANGE = re.compile(r"\b(?:MSNs?|serial numbers?)\s*(\d{2,6})\s*(?:to|through|-|until)\s*(\d{2,6})", re.I)
AIRBUS_MODEL = re.compile(r"\b(A3[0-9]\d(?:-[0-9A-Z]{2,5})?|A220(?:-\d{3})?|BD-500-1A1[01])\b", re.I)
OTHER_MODEL = re.compile(r"\b((?:Boeing\s+)?7[0-8]7(?:-\d{1,3}[A-Z]*)?|ATR\s?\d{2}|E1[79]\d|ERJ-?\d{3}|CRJ\d{3,4}|Cessna\s+\d{3})\b", re.I)
APPLIC = re.compile(r"\b(appl(?:y|ies|icable|icability)|affect(?:s|ed)?|impact(?:ed)?|cover(?:s|ed)?|kena|berlaku|my fleet|our fleet)\b", re.I)
REVISION = re.compile(r"\b(latest|versions?|revisions?|changed?|changes|differ\w*|revised|supersed\w*|withdrawn|withdraw\w*|history|replace[sd]?)\b", re.I)
JUDGEMENT = re.compile(r"\b(safe to fly|is it safe|should (?:i|we)|recommend|legal advice|ground the)\b", re.I)
PREMISE = re.compile(r"\b(since|given that|because|as we know|knowing that)\b", re.I)

ANSWER_SYSTEM = """You answer questions about FAA airworthiness directive documents.
Use ONLY the numbered source chunks provided. Never use outside knowledge. Never guess.
Return claims through the tool. Each claim: one short sentence, the chunk_id it comes from, and a quote copied EXACTLY from that chunk (8-300 characters) that proves the sentence.
If the chunks do not contain the answer, return insufficient=true and no claims."""

ANSWER_TOOL = {
    "name": "emit_answer", "description": "Return cited claims.",
    "input_schema": {"type": "object", "properties": {
        "insufficient": {"type": "boolean"},
        "claims": {"type": "array", "items": {"type": "object", "properties": {
            "text": {"type": "string"}, "chunk_id": {"type": "string"}, "quote": {"type": "string"}},
            "required": ["text", "chunk_id", "quote"]}}},
        "required": ["insufficient", "claims"]},
}


def _client():
    if not os.getenv("ANTHROPIC_API_KEY"):
        return None
    import anthropic
    return anthropic.Anthropic()


def parse(q, fleet):
    regs = {a["registration"].upper(): a for a in fleet}
    p = {
        "dockets": [d.upper() for d in DOCKET.findall(q)],
        "doc_numbers": DOCNUM.findall(q),
        "ad_numbers": ADNUM.findall(q),
        "registrations": [r.upper() for r in REG.findall(q)],
        "msns": MSN.findall(q),
        "msn_ranges": [(int(a), int(b)) for a, b in MSN_RANGE.findall(q)],
        "airbus_models": [m.upper() for m in AIRBUS_MODEL.findall(q)],
        "other_models": OTHER_MODEL.findall(q),
        "premise": bool(PREMISE.search(q)),
        "judgement": bool(JUDGEMENT.search(q)),
    }
    p["aircraft"] = [regs[r] for r in p["registrations"] if r in regs]
    p["aircraft"] += [a for a in fleet if a["msn"] in p["msns"] and a not in p["aircraft"]]
    p["unknown_registrations"] = [r for r in p["registrations"] if r not in regs]
    if APPLIC.search(q) or p["aircraft"]:
        p["intent"] = "applicability"
    elif REVISION.search(q):
        p["intent"] = "revision"
    else:
        p["intent"] = "lookup"
    return p


def resolve_docs(con, p):
    ids = []
    for d in p["dockets"]:
        ids += [r["doc_id"] for r in rows(con, "SELECT doc_id FROM revisions WHERE docket=? OR docket=? ORDER BY version_index", (d, re.sub(r"R1$", "", d)))]
    for n in p["doc_numbers"]:
        ids += [r["doc_id"] for r in rows(con, "SELECT doc_id FROM documents WHERE doc_number=? OR doc_id LIKE ?", (n, n + "%"))]
    for a in p["ad_numbers"]:
        ids += [r["doc_id"] for r in rows(con, "SELECT doc_id FROM documents WHERE final_ad_number=? OR supersedes_ad LIKE ?", (a, f"%{a}%"))]
    return list(dict.fromkeys(ids))


def latest_of(con, doc_ids):
    out = []
    for d in doc_ids:
        r = one(con, "SELECT latest_doc_id FROM revisions WHERE doc_id=?", (d,))
        out.append(r["latest_doc_id"] if r else d)
    return list(dict.fromkeys(out))


def refuse(code, msg, **kw):
    return {"status": "REFUSED" if code not in ("R3", "R4") else "NEEDS_INFO", "refusal_code": code, "message": msg,
            "claims": [], "verdicts": [], "warnings": [], **kw}


def cite(con, chunk_id, quote, text=None):
    c = one(con, """SELECT c.chunk_id, c.doc_id, c.section, c.page, d.docket, d.url, d.fr_citation
                    FROM chunks c JOIN documents d USING(doc_id) WHERE chunk_id=?""", (chunk_id,))
    return {"text": text, "quote": quote, **(c or {"chunk_id": chunk_id})}


def rule_chunk_id(con, doc_id):
    c = one(con, "SELECT chunk_id FROM chunks WHERE doc_id=? AND section LIKE '%Applicability%' ORDER BY ord", (doc_id,))
    if c:
        return c["chunk_id"]
    c = one(con, "SELECT chunk_id FROM chunks WHERE doc_id=? AND section='Summary (dataset)'", (doc_id,))
    return c["chunk_id"] if c else None


def check_premise(con, p, doc_ids):
    """R6: deterministic checks of facts the user asserts."""
    if not p["premise"] or not doc_ids:
        return None
    for d in doc_ids:
        rule, _ = get_rule(con, d)
        if not rule:
            continue
        ranges = [(c.min, c.max) for g in rule.groups for c in g.conditions if c.field == "msn" and c.op == "range"]
        for r in p["msn_ranges"]:
            if r not in ranges:
                return f"The question assumes MSN range {r[0]}-{r[1]}, but the applicability of {d} states " + (
                    f"{ranges}." if ranges else "no MSN range.")
        models = {norm_model(v) for g in rule.groups for c in g.conditions if c.field == "model" for v in c.values}
        fams = {norm_model(v) for g in rule.groups for c in g.conditions if c.field == "model_family" for v in c.values}
        for m in p["airbus_models"]:
            if "-" in m and (models or fams) and norm_model(m) not in models and family(m) not in fams:
                return f"The question assumes {d} covers {m}, but its applicability lists {sorted(models)}."
    return None


def answer_applicability(con, p, fleet, doc_ids, out):
    targets = p["aircraft"]
    if not doc_ids and targets:
        out["verdicts"] = [{"registration": a["registration"], "directives": aircraft_report(con, a)} for a in targets]
        out["status"] = "ANSWER"
        out["message"] = "Directives that apply or cannot be ruled out, per aircraft (latest versions, not withdrawn)."
        return out
    if not doc_ids:
        if p["airbus_models"]:
            fam = {family(m) for m in p["airbus_models"]}
            cands = [a["registration"] for a in fleet if family(a["model"]) in fam]
            return refuse("R3", "Which directive and which aircraft? Several directives and fleet aircraft match "
                                f"{', '.join(p['airbus_models'])}. Fleet candidates: {', '.join(cands) or 'none'}.", parsed=p)
        return refuse("R3", "Which directive (docket number) and which aircraft do you mean?", parsed=p)
    if not targets and p["airbus_models"]:
        targets = [{"registration": f"(any {m})", "model": m, "msn": None, "engine_model": None,
                    "mods_installed": None, "parts_installed": None} for m in p["airbus_models"] if "-" in m]
    latest = latest_of(con, doc_ids)
    for d in latest:
        st = doc_status(con, d)
        out["warnings"] += st["warnings"]
        if st["revision"] and st["revision"]["docket_status"] == "WITHDRAWN":
            for a in targets or fleet:
                out["verdicts"].append({"doc_id": d, "registration": a["registration"], "model": a["model"], "msn": a["msn"],
                                        "label": "NOT APPLICABLE", "missing": [], "citations": [],
                                        "evidence": [{"quote": None, "result": "FALSE", "reason": "docket withdrawn"}]})
            wc = one(con, "SELECT chunk_id, text FROM chunks WHERE doc_id=? ORDER BY ord LIMIT 1", (d,))
            if wc:
                out["claims"].append(cite(con, wc["chunk_id"], re.split(r"(?<=\.)\s", wc["text"])[0][:300], "The proposal was withdrawn."))
            continue
        rep = impact_report(con, d, targets or fleet)
        if rep.get("error"):
            return refuse("R4", f"{d}: {rep['error']} Cannot evaluate applicability.", parsed=p, warnings=st["warnings"])
        cid = rule_chunk_id(con, d)
        for a in rep["aircraft"]:
            ev = [cite(con, cid, e["quote"], e["reason"]) for e in a["evidence"] if e["quote"]]
            out["verdicts"].append({"doc_id": d, **a, "citations": ev})
    labels = {v["label"] for v in out["verdicts"]}
    out["status"] = "NEEDS_INFO" if labels == {"CANNOT DETERMINE"} else "ANSWER"
    out["message"] = "Applicability decided by the rule engine (deterministic), not by the language model."
    return out


def llm_claims(client, q, chunks):
    ctx = "\n\n".join(f"[{c['chunk_id']}] ({c['section']}, page {c['page']})\n{c['text']}" for c in chunks)
    msg = client.messages.create(model=MODEL_ANSWER, max_tokens=1500, system=ANSWER_SYSTEM, tools=[ANSWER_TOOL],
                                 tool_choice={"type": "tool", "name": "emit_answer"},
                                 messages=[{"role": "user", "content": f"QUESTION: {q}\n\nSOURCE CHUNKS:\n{ctx}"}])
    data = next(b.input for b in msg.content if b.type == "tool_use")
    return data.get("insufficient", False), data.get("claims", [])


def terms(q):
    from .retrieve import STOP
    return {t.lower() for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", q) if t.lower() not in STOP and len(t) > 2}


def coverage(q, chunks):
    ts = terms(q)
    if not ts:
        return 0.0
    blob = " ".join(c["text"].lower() for c in chunks)
    return sum(1 for t in ts if t in blob or t.rstrip("s") in blob) / len(ts)


def extractive_claims(q, chunks, n=3):
    ts, out = terms(q), []
    for c in chunks[:n]:
        sent = [x for x in re.split(r"(?<=[.;])\s+", c["text"].strip()) if len(x) > 30]
        if not sent:
            continue
        s = max(sent, key=lambda x: sum(t in x.lower() for t in ts))[:300]
        if any(o["quote"] == s for o in out):
            continue
        out.append({"text": s, "chunk_id": c["chunk_id"], "quote": s})
    return out


def answer_lookup(con, q, p, doc_ids, out, client):
    chunks, rt = search(con, q, doc_ids=doc_ids or None)
    out["latency"]["retrieval_s"] = rt
    if not chunks or chunks[0]["score"] < BM25_MIN_SCORE or coverage(q, chunks[:3]) < 0.6:
        return refuse("R1", "No document in the dataset covers this question.", parsed=p, latency=out["latency"])
    for d in dict.fromkeys(c["doc_id"] for c in chunks[:3]):
        out["warnings"] += [f"{d}: {w}" for w in doc_status(con, d)["warnings"] if not w.startswith("PROPOSED")]
    out["warnings"].insert(0, "All documents in this dataset are proposed rules (NPRMs) unless stated otherwise.")
    for attempt in range(2):
        if client:
            insufficient, claims = llm_claims(client, q, chunks)
            if insufficient:
                return refuse("R1", "The retrieved documents do not contain the answer.", parsed=p, latency=out["latency"],
                              retrieved=[c["chunk_id"] for c in chunks])
        else:
            claims, out["mode"] = extractive_claims(q, chunks), "extractive (no API key)"
        errs = verify(claims, chunks)
        if not errs:
            out["claims"] = [cite(con, c["chunk_id"], c["quote"], c["text"]) for c in claims]
            out["status"], out["message"] = "ANSWER", ""
            return out
        out["verifier_errors"] = errs
    return refuse("R5", "Could not produce a verifiable answer.", parsed=p, latency=out["latency"],
                  retrieved=[c["chunk_id"] for c in chunks], verifier_errors=errs)


def answer_revision(con, p, doc_ids, out, fleet):
    from ingest.revisions import history
    if not doc_ids:
        return refuse("R3", "Which docket or document number do you mean?", parsed=p)
    seen = set()
    for d in doc_ids:
        h = history(con, d)
        key = h[0]["docket"] if h else d
        if key in seen:
            continue
        seen.add(key)
        from adc.fleet import version_diff
        out["verdicts"].append({"docket": key, "history": h, "diffs": version_diff(con, d, fleet)})
        out["warnings"] += doc_status(con, h[-1]["doc_id"] if h else d)["warnings"]
        for v in h:
            cid = one(con, "SELECT chunk_id FROM chunks WHERE doc_id=? AND section='Summary (dataset)'", (v["doc_id"],))
            txt = one(con, "SELECT text FROM chunks WHERE chunk_id=?", (cid["chunk_id"],))["text"] if cid else ""
            q = re.split(r"(?<=\.)\s", txt)[0][:300] if txt else ""
            out["claims"].append(cite(con, cid["chunk_id"], q, f"Version {v['version_index']}/{v['n_versions']}: {v['doc_type']} issued {v['issue_date']}"))
    out["status"] = "ANSWER"
    return out


def ask(con, q, fleet):
    t0 = time.perf_counter()
    p = parse(q, fleet)
    out = {"status": None, "refusal_code": None, "message": "", "claims": [], "verdicts": [], "warnings": [],
           "parsed": {k: v for k, v in p.items() if k != "aircraft"}, "latency": {}}

    def done(r):
        r.setdefault("latency", {})["total_s"] = time.perf_counter() - t0
        r.setdefault("parsed", out["parsed"])
        return r

    if p["judgement"]:
        return done(refuse("R1", "This asks for an operational or safety judgement. The system only reports what the documents say; the decision stays with the engineer.", parsed=out["parsed"]))
    if p["unknown_registrations"]:
        return done(refuse("R2", f"Aircraft {', '.join(p['unknown_registrations'])} is not in the fleet database.", parsed=out["parsed"]))
    doc_ids = resolve_docs(con, p)
    asked = p["dockets"] + p["doc_numbers"] + p["ad_numbers"]
    if asked and not doc_ids:
        return done(refuse("R2", f"{', '.join(asked)} is not in the dataset.", parsed=out["parsed"]))
    if p["other_models"] and p["intent"] == "applicability":
        return done(refuse("R2", f"Applicability checking covers Airbus aircraft only; {', '.join(p['other_models'])} is out of scope. Search is still available.", parsed=out["parsed"]))
    contradiction = check_premise(con, p, latest_of(con, doc_ids))
    if contradiction:
        return done(refuse("R6", contradiction, parsed=out["parsed"]))
    if p["airbus_models"]:
        unknown = [m for m in p["airbus_models"] if "-" in m and not one(con, "SELECT 1 FROM chunks_fts WHERE chunks_fts MATCH ?", (f'"{m}"',))]
        if unknown:
            return done(refuse("R2", f"Model {', '.join(unknown)} does not appear in any document in the dataset.", parsed=out["parsed"]))
    if p["intent"] == "applicability":
        return done(answer_applicability(con, p, fleet, doc_ids, out))
    if p["intent"] == "revision":
        return done(answer_revision(con, p, doc_ids, out, fleet))
    return done(answer_lookup(con, q, p, doc_ids, out, _client()))
