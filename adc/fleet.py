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
                "mods_installed": _list(r.get("mods_installed")), "parts_installed": _list(r.get("parts_installed")),
            })
    return good, bad


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
            "evidence": [{"quote": t.condition.quote, "result": t.result, "reason": t.reason} for t in v.traces],
            "missing": v.missing,
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
