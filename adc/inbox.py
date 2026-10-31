"""Inbox cards: one plain-language line per directive, built only from FAA text + rule engine output."""
import difflib
import re
from datetime import date

from .actions import required_actions
from .db import one, rows
from .fleet import impact_report, signoffs, version_diff

PROMPTED = re.compile(r"(?:This (?:proposed )?AD|This action|The NPRM) was prompted by ([^.]+)\.|Since the FAA issued AD [\d-]+, ([^.]+)\.", re.I)
REQUIRE = re.compile(r"This proposed AD would (?:also |continue to )?require ([^.]+)\.", re.I)


def prompted_by(summary):
    m = PROMPTED.search(summary or "")
    return (m.group(1) or m.group(2)).strip() if m else None


def would_require(summary):
    m = REQUIRE.search(summary or "")
    return m.group(1).strip() if m else None


def changed_words(a, b, limit=3):
    """Word-level changes between two texts, e.g. [('October 1, 2024', 'December 19, 2025')]."""
    wa, wb = " ".join(a.split()).split(), " ".join(b.split()).split()
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes():
        if tag != "equal":
            out.append((" ".join(wa[i1:i2]), " ".join(wb[j1:j2])))
    return out[:limit]


def card(con, doc_id, fleet, today):
    rep = impact_report(con, doc_id, fleet)
    if rep.get("error"):
        return None
    d, rv = rep["status"]["doc"], rep["status"]["revision"]
    hit = [a for a in rep["aircraft"] if a["label"] == "APPLIES"]
    unk = [a for a in rep["aircraft"] if a["label"] == "CANNOT DETERMINE"]
    so = signoffs(con, doc_id)
    relevant = hit + unk
    done = bool(relevant) and all(a["registration"] in so for a in relevant) and not unk
    due = d["comments_due"]
    days = (date.fromisoformat(due) - date.fromisoformat(today)).days if due else None
    act = required_actions(con, doc_id)
    revised = None
    if rv and rv["n_versions"] > 1:
        diffs = version_diff(con, doc_id, fleet)
        if diffs:
            last = diffs[-1]
            from rules.extract import applicability_text
            a_txt = applicability_text(con, last["from"])[1]
            b_txt = applicability_text(con, last["to"])[1]
            revised = {"from": last["from_date"], "to": last["to_date"], "fleet_changes": last["fleet_changes"],
                       "words": changed_words(a_txt, b_txt) if last["applicability_changes"] else []}
    kind = "needs_input" if unk else ("affects" if hit else "clear")
    return {
        "doc_id": doc_id, "docket": d["docket"], "kind": kind, "done": done,
        "applies": [a["registration"] for a in hit], "unknown": [a["registration"] for a in unk],
        "prompted": prompted_by(d["summary"]), "require": would_require(d["summary"]),
        "due": due, "days_left": days, "status": rv["docket_status"] if rv else None,
        "effort": act["effort"]["max_work_hours"] if act["effort"] else None,
        "first_time": act["compliance_times"][0]["text"] if act["compliance_times"] else None,
        "revised": revised, "n_questions": sum(len(a["questions"]) for a in unk),
    }


def build_inbox(con, fleet, today=None):
    today = today or date.today().isoformat()
    ids = [r["doc_id"] for r in rows(con, """SELECT r.doc_id FROM rules r JOIN revisions v USING(doc_id) JOIN documents d USING(doc_id)
                                              WHERE v.is_latest=1 AND v.docket_status!='WITHDRAWN'
                                              AND d.make_norm IN ('Airbus','Airbus Canada')""")]
    cards = [c for c in (card(con, i, fleet, today) for i in ids) if c]
    open_ = [c for c in cards if c["days_left"] is not None and c["days_left"] >= 0]
    older = [c for c in cards if c not in open_]
    key = lambda c: (c["done"], {"needs_input": 0, "affects": 1, "clear": 2}[c["kind"]], c["days_left"] if c["days_left"] is not None else 9999)
    return sorted(open_, key=key), sorted(older, key=lambda c: c["due"] or "", reverse=True)
