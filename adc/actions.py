"""What would the engineer have to do: required-action sections, compliance times, estimated work-hours.
Pure section selection + regex. No LLM. Every item carries its chunk_id."""
import re

from .db import rows

SKIP = re.compile(r"AMOC|Alternative Methods|Additional|Material Incorporated|Credit for Previous|Special Flight|Reporting|"
                  r"Related Information|Other FAA|Provisions|Exceptions|Comments Due|Applicability|Subject|Unsafe|Reason|Affected", re.I)
ACTION = re.compile(r"Requirement|Required|Action|Modification|Retained|New |Inspection|Replace|Revision|Repetitive|Installation", re.I)
TIME = re.compile(r"(?:within|before further flight|before exceeding|before the accumulation of|at intervals not to exceed|"
                  r"prior to|thereafter at intervals of)[^.;]{0,140}?(?:flight hours|flight cycles|days|months|years|landings|further flight)", re.I)
HOURS = re.compile(r"(?:up to\s+)?(\d[\d,.]*)\s+work-hours", re.I)


def required_actions(con, doc_id):
    chunks = rows(con, "SELECT chunk_id, section, page, text FROM chunks WHERE doc_id=? ORDER BY ord", (doc_id,))
    picked = []
    for c in chunks:
        s = c["section"]
        para = re.match(r"^\(([a-z])\)\s", s)
        if s == "Proposed AD Requirements":
            picked.append({**c, "kind": "summary"})
        elif para and para.group(1) >= "g" and ACTION.search(s) and not SKIP.search(s):
            picked.append({**c, "kind": "regulatory text"})
        elif s == "Compliance" and "(a)" in c["text"]:
            picked.append({**c, "kind": "regulatory text"})
    times = []
    for c in picked:
        for m in TIME.finditer(" ".join(c["text"].split())):
            t = m.group(0).strip()
            if t not in [x["text"] for x in times]:
                times.append({"text": t, "chunk_id": c["chunk_id"]})
    hours = None
    cost = next((c for c in chunks if c["section"].startswith("Costs of Compliance")), None)
    if cost:
        hs = [float(h.replace(",", "")) for h in HOURS.findall(cost["text"])]
        if hs:
            hours = {"max_work_hours": max(hs), "chunk_id": cost["chunk_id"]}
    return {"sections": picked, "compliance_times": times[:8], "effort": hours}
