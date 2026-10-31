"""APP-5: sample 10 fixed-wing Airbus rules (random, seed 0) and dump source text next to the extracted rule for manual checking."""
import csv
import json
import random
from pathlib import Path

from adc.db import connect, rows
from rules.extract import applicability_text


def summarize(rule):
    parts = []
    for i, g in enumerate(rule["groups"]):
        conds = []
        for c in g["conditions"]:
            if c["op"] == "range":
                conds.append(f"{c['field']} {c['min']}..{c['max']}")
            else:
                conds.append(f"{c['field']} {c['op']} {c['values']}")
        parts.append(f"G{i + 1}: " + " AND ".join(conds))
    for c in rule["exclusions"]:
        parts.append(f"EXCEPT {c['field']} {c['op']} {c['values']}")
    for u in rule["unresolved"]:
        parts.append(f"UNRESOLVED: {u[:120]}")
    return " | ".join(parts)


def main():
    con = connect()
    pool = rows(con, """SELECT r.doc_id, d.docket, r.validation, r.rule_json FROM rules r JOIN documents d USING(doc_id)
                        WHERE d.make_norm IN ('Airbus','Airbus Canada') AND r.validation IN ('auto_validated','partial')
                        ORDER BY r.doc_id""")
    random.seed(0)
    sample = random.sample(pool, min(10, len(pool)))
    out = Path(__file__).parent / "rule_check.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["doc_id", "docket", "validation", "source_applicability", "extracted_rule", "correct (Y/N/partial)", "note"])
        for r in sample:
            _, src = applicability_text(con, r["doc_id"])
            w.writerow([r["doc_id"], r["docket"], r["validation"], " ".join(src.split())[:1500], summarize(json.loads(r["rule_json"])), "", ""])
    print(f"wrote {out} ({len(sample)} rules from a pool of {len(pool)})")


if __name__ == "__main__":
    main()
