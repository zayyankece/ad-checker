"""FLT-1..5: fleet loading and impact reports."""
import csv
import json

from rules.engine import evaluate
from rules.schema import Rule

from .config import FLEET
from .db import one, rows

REQUIRED = ["registration", "model", "msn"]


def _list(v):
    v = (v or "").strip()
    if v.lower() == "unknown":
        return None
    return [x.strip() for x in v.split(";") if x.strip()]


def load_fleet(path=FLEET):
    good, bad = [], []
    with open(path) as f:
        for i, r in enumerate(csv.DictReader(f), 2):
            miss = [k for k in REQUIRED if not (r.get(k) or "").strip()]
            if miss:
                bad.append((i, f"missing {miss}"))
                continue
            good.append({
                "registration": r["registration"].strip(), "model": r["model"].strip(), "msn": r["msn"].strip(),
                "engine_model": (r.get("engine_model") or "").strip() or None,
                "cert_date": (r.get("cert_date") or "").strip() or None,
                "mods_installed": _list(r.get("mods_installed")), "parts_installed": _list(r.get("parts_installed")),
            })
    return good, bad


def plain_reason(v):
    """One sentence an engineer can read in two seconds."""
    rs = [t.reason for t in v.traces]
    if v.label == "APPLIES":
        return "; ".join(t.reason for t in v.traces if t.result == "TRUE")
    if v.label == "NOT APPLICABLE":
        return next((t.reason for t in v.traces if t.result == "FALSE"), "not covered")
    return "Need info: " + (v.questions[0]["text"] if v.questions else "; ".join(rs))


def load_fleet_db(con):
    """Live fleet (editable in the app). Seeded from fleet.csv the first time."""
    if not one(con, "SELECT 1 FROM aircraft LIMIT 1"):
        seed, _ = load_fleet()
        for a in seed:
            upsert_aircraft(con, a)
    out = []
    for r in rows(con, "SELECT * FROM aircraft ORDER BY registration"):
        out.append({**r, "mods_installed": json.loads(r["mods_installed"]) if r["mods_installed"] else None,
                    "parts_installed": json.loads(r["parts_installed"]) if r["parts_installed"] else None,
                    "facts": json.loads(r["facts"] or "{}")})
    return out


def upsert_aircraft(con, a):
    old = one(con, "SELECT facts FROM aircraft WHERE registration=?", (a["registration"],))
    facts = a.get("facts") or (json.loads(old["facts"]) if old and old["facts"] else {})
    con.execute("INSERT OR REPLACE INTO aircraft VALUES (?,?,?,?,?,?,?,?)", (
        a["registration"].strip().upper(), a["model"].strip().upper(), str(a.get("msn") or "").strip() or None,
        a.get("engine_model") or None, a.get("cert_date") or None,
        json.dumps(a["mods_installed"]) if a.get("mods_installed") is not None else None,
        json.dumps(a["parts_installed"]) if a.get("parts_installed") is not None else None, json.dumps(facts)))
    con.commit()


def delete_aircraft(con, reg):
    con.execute("DELETE FROM aircraft WHERE registration=?", (reg,))
    con.commit()


def answer_question(con, reg, key, value):
    """Store an engineer's answer. Plain fields update the aircraft record; everything else becomes a fact."""
    if key in ("msn", "cert_date", "model", "engine_model"):
        con.execute(f"UPDATE aircraft SET {key}=? WHERE registration=?", (value, reg))
    else:
        r = one(con, "SELECT facts FROM aircraft WHERE registration=?", (reg,))
        facts = json.loads(r["facts"] or "{}")
        facts[key] = value
        con.execute("UPDATE aircraft SET facts=? WHERE registration=?", (json.dumps(facts), reg))
    con.commit()


def doc_status(con, doc_id):
    d = one(con, "SELECT * FROM documents WHERE doc_id=?", (doc_id,))
    rv = one(con, "SELECT * FROM revisions WHERE doc_id=?", (doc_id,))
    warnings = []
    if d["doc_type"] == "WITHDRAWAL" or (rv and rv["docket_status"] == "WITHDRAWN"):
        warnings.append(f"Docket {d['docket']} was WITHDRAWN (latest document: {rv['latest_doc_id']}). It does not apply.")
    elif rv and rv["docket_status"] == "ADOPTED":
        final = one(con, "SELECT final_ad_number FROM documents d JOIN revisions r USING(doc_id) WHERE r.docket=? AND final_ad_number IS NOT NULL", (rv["docket"],))
        warnings.append(f"This proposal was adopted as final AD {final['final_ad_number']}. Check the final rule text; it may differ from the proposal.")
    else:
        warnings.append("PROPOSED rule (NPRM). Not yet binding.")
    if rv and not rv["is_latest"]:
        warnings.append(f"Not the latest version: version {rv['version_index']} of {rv['n_versions']} in docket {rv['docket']}. Latest: {rv['latest_doc_id']}.")
    if d["supersedes_ad"]:
        warnings.append(f"Would supersede AD {d['supersedes_ad']}.")
    return {"doc": d, "revision": rv, "warnings": warnings}


def get_rule(con, doc_id):
    r = one(con, "SELECT * FROM rules WHERE doc_id=?", (doc_id,))
    if not r:
        return None, None
    return Rule.model_validate_json(r["rule_json"]), r


def impact_report(con, doc_id, fleet):
    rule, meta = get_rule(con, doc_id)
    st = doc_status(con, doc_id)
    out = {"doc_id": doc_id, "status": st, "rule_meta": meta, "aircraft": []}
    if rule is None:
        out["error"] = "No applicability rule extracted for this document."
        return out
    for ac in fleet:
        v = evaluate(rule, ac)
        if meta["validation"] == "needs_review" and v.label != "NOT APPLICABLE":
            v.label, v.value = "CANNOT DETERMINE", "UNKNOWN"
            v.missing.insert(0, "Applicability rule failed validation; engineer review required.")
        out["aircraft"].append({
            "registration": ac["registration"], "model": ac["model"], "msn": ac["msn"], "label": v.label,
            "evidence": [{"quote": t.condition.quote if t.condition else None, "result": t.result, "reason": t.reason} for t in v.traces],
            "missing": v.missing, "questions": v.questions, "reason": plain_reason(v),
        })
    return out


def aircraft_report(con, ac):
    out = []
    for r in rows(con, "SELECT r.doc_id FROM rules r JOIN revisions v USING(doc_id) WHERE v.is_latest=1 AND v.docket_status!='WITHDRAWN'"):
        rule, meta = get_rule(con, r["doc_id"])
        v = evaluate(rule, ac)
        if v.label != "NOT APPLICABLE":
            d = one(con, "SELECT docket, subject, comments_due FROM documents WHERE doc_id=?", (r["doc_id"],))
            out.append({"doc_id": r["doc_id"], **d, "label": v.label, "missing": v.missing, "validation": meta["validation"]})
    return sorted(out, key=lambda x: (x["label"] != "APPLIES", x["comments_due"] or ""), reverse=False)


def version_diff(con, doc_id, fleet):
    """REV-5: what changed between consecutive versions of a docket, and the fleet impact delta."""
    import difflib

    from ingest.revisions import history
    from rules.extract import applicability_text

    h = [v for v in history(con, doc_id) if v["doc_type"] != "WITHDRAWAL"]
    out = []
    for old, new in zip(h, h[1:]):
        a_cid, a_txt = applicability_text(con, old["doc_id"])
        b_cid, b_txt = applicability_text(con, new["doc_id"])
        norm = lambda t: [x.strip() for x in __import__("re").split(r"(?<=[.;])\s+|\n\s*\n", " ".join(t.split())) if x.strip()]
        text_changes = [d for d in difflib.unified_diff(norm(a_txt), norm(b_txt), lineterm="", n=0) if d[:1] in "+-" and d[:3] not in ("+++", "---")]
        sec = lambda d: {r["section"] for r in rows(con, "SELECT DISTINCT section FROM chunks WHERE doc_id=?", (d,))}
        sa, sb = sec(old["doc_id"]), sec(new["doc_id"])
        ra, rb = impact_report(con, old["doc_id"], fleet), impact_report(con, new["doc_id"], fleet)
        la = {x["registration"]: x["label"] for x in ra.get("aircraft", [])}
        lb = {x["registration"]: x["label"] for x in rb.get("aircraft", [])}
        changed = [{"registration": r, "before": la.get(r), "after": lb.get(r)} for r in lb if la.get(r) != lb.get(r)]
        out.append({"from": old["doc_id"], "to": new["doc_id"], "from_date": old["issue_date"], "to_date": new["issue_date"],
                    "applicability_changes": text_changes, "applicability_chunks": [a_cid, b_cid],
                    "sections_added": sorted(sb - sa), "sections_removed": sorted(sa - sb), "fleet_changes": changed})
    return out


def review_queue(con, fleet, today=None):
    """Weekly worklist: latest, non-withdrawn proposals that hit or may hit the fleet, soonest comment deadline first."""
    from datetime import date
    today = today or date.today().isoformat()
    out = []
    for r in rows(con, """SELECT r.doc_id FROM rules r JOIN revisions v USING(doc_id) JOIN documents d USING(doc_id)
                          WHERE v.is_latest=1 AND v.docket_status!='WITHDRAWN' AND d.make_norm IN ('Airbus','Airbus Canada')"""):
        rep = impact_report(con, r["doc_id"], fleet)
        hit = [a for a in rep["aircraft"] if a["label"] == "APPLIES"]
        unk = [a for a in rep["aircraft"] if a["label"] == "CANNOT DETERMINE"]
        if not hit and not unk:
            continue
        d = rep["status"]["doc"]
        due = d["comments_due"]
        days = (date.fromisoformat(due) - date.fromisoformat(today)).days if due else None
        signed = one(con, "SELECT COUNT(*) n FROM signoffs WHERE doc_id=?", (r["doc_id"],))["n"]
        out.append({
            "docket": d["docket"], "doc_id": r["doc_id"], "status": rep["status"]["revision"]["docket_status"],
            "comments_due": due, "days_left": days,
            "applies": ", ".join(a["registration"] for a in hit), "cannot_determine": ", ".join(a["registration"] for a in unk),
            "signed_off": f"{signed}/{len(hit) + len(unk)}", "subject": (d["summary"] or "")[:160],
        })
    open_first = sorted([x for x in out if x["days_left"] is not None and x["days_left"] >= 0], key=lambda x: x["days_left"])
    rest = sorted([x for x in out if x not in open_first], key=lambda x: x["comments_due"] or "", reverse=True)
    return open_first + rest


def sign_off(con, doc_id, registration, system_label, decision, final_label, note, engineer):
    from datetime import datetime
    con.execute("INSERT OR REPLACE INTO signoffs VALUES (?,?,?,?,?,?,?,?)",
                (doc_id, registration, system_label, decision, final_label, note, engineer, datetime.now().isoformat(timespec="seconds")))
    con.commit()


def signoffs(con, doc_id):
    return {r["registration"]: r for r in rows(con, "SELECT * FROM signoffs WHERE doc_id=?", (doc_id,))}
