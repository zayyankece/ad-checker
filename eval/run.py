"""Run golden set against the system and baselines. python -m eval.run [--baselines]"""
import argparse
import json
import os
import re
import statistics as stt
import time
from collections import defaultdict
from pathlib import Path

from adc.config import MODEL_ANSWER
from adc.db import connect
from adc.fleet import load_fleet
from qa.pipeline import ask
from qa.retrieve import search

HERE = Path(__file__).parent
DECLINE = re.compile(r"\b(not (?:in|found|available|able|contain)|cannot|can't|unable|no information|don't have|do not have|insufficient|not provided)\b", re.I)


def check(case, r):
    fails = []
    if "expect_status" in case and r["status"] != case["expect_status"]:
        fails.append(f"status {r['status']} != {case['expect_status']}")
    if "expect_status_in" in case and r["status"] not in case["expect_status_in"]:
        fails.append(f"status {r['status']} not in {case['expect_status_in']}")
    if "expect_code" in case and r["refusal_code"] != case["expect_code"]:
        fails.append(f"code {r['refusal_code']} != {case['expect_code']}")
    labels = {v.get("registration"): v.get("label") for v in r["verdicts"] if "label" in v}
    for reg, lab in case.get("expect_labels", {}).items():
        if labels.get(reg) != lab:
            fails.append(f"{reg}: {labels.get(reg)} != {lab}")
    for reg, labs in case.get("expect_labels_in", {}).items():
        if labels.get(reg) not in labs:
            fails.append(f"{reg}: {labels.get(reg)} not in {labs}")
    if "expect_doc" in case:
        dockets = {c.get("docket") for c in r["claims"]} | {v.get("docket") for v in r["verdicts"]}
        if case["expect_doc"] not in dockets:
            fails.append(f"expected docket {case['expect_doc']} not cited (got {sorted(d for d in dockets if d)[:4]})")
    if "expect_warning" in case and not any(case["expect_warning"] in w for w in r["warnings"]):
        fails.append(f"missing warning {case['expect_warning']}")
    if "expect_versions" in case:
        h = next((v["history"] for v in r["verdicts"] if "history" in v), [])
        if len(h) != case["expect_versions"]:
            fails.append(f"versions {len(h)} != {case['expect_versions']}")
    if "expect_fleet_change" in case:
        diffs = [d for v in r["verdicts"] for d in v.get("diffs", [])]
        got = {c["registration"]: c["after"] for d in diffs for c in d["fleet_changes"]}
        for reg, lab in case["expect_fleet_change"].items():
            if got.get(reg) != lab:
                fails.append(f"fleet change {reg}: {got.get(reg)} != {lab}")
    return fails


def baselines(con, cases):
    from adc.llm import client, text
    cl = client()
    out = []
    for c in cases:
        chunks, _ = search(con, c["q"], k=5)
        ctx = "\n\n".join(f"[{x['chunk_id']}] {x['text'][:1500]}" for x in chunks)
        try:
            b0 = text(cl.messages.create(model=MODEL_ANSWER, max_tokens=4000, messages=[{"role": "user", "content": c["q"]}]))
            b1 = text(cl.messages.create(model=MODEL_ANSWER, max_tokens=4000, messages=[{"role": "user", "content": f"Context:\n{ctx}\n\nQuestion: {c['q']}"}]))
        except Exception as e:
            print("baseline error", c["id"], str(e)[:120])
            continue
        out.append({"id": c["id"], "cat": c["cat"], "q": c["q"], "B0": b0, "B1": b1})
        print("baseline", c["id"])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--baselines", action="store_true")
    a = ap.parse_args()
    con = connect()
    fleet, _ = load_fleet()
    cases = [json.loads(l) for l in open(HERE / "golden.jsonl") if l.strip()]
    res, per_cat, lat_total, lat_ret = [], defaultdict(lambda: [0, 0]), [], []
    for c in cases:
        r = ask(con, c["q"], fleet)
        f = check(c, r)
        per_cat[c["cat"]][0] += not f
        per_cat[c["cat"]][1] += 1
        lat_total.append(r["latency"]["total_s"])
        t0 = time.perf_counter(); search(con, c["q"]); lat_ret.append(time.perf_counter() - t0)
        res.append({"id": c["id"], "cat": c["cat"], "pass": not f, "fails": f, "status": r["status"], "code": r["refusal_code"], "llm_error": r.get("llm_error")})
        print(("PASS" if not f else "FAIL"), c["id"], c["q"][:70], f)
    exp_ref = [c for c in cases if c.get("expect_status") in ("REFUSED", "NEEDS_INFO") or set(c.get("expect_status_in", [])) & {"REFUSED", "NEEDS_INFO"}]
    got_ref = [x for x in res if x["status"] in ("REFUSED", "NEEDS_INFO")]
    tp = sum(1 for x in got_ref if any(c["id"] == x["id"] for c in exp_ref))
    q = lambda xs, p: sorted(xs)[min(len(xs) - 1, int(p * len(xs)))]
    n_chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    n_pages = con.execute("SELECT SUM(fr_page_end - fr_page_start + 1) FROM documents").fetchone()[0]
    summary = {
        "cases": len(cases), "passed": sum(x["pass"] for x in res),
        "per_category": {k: f"{v[0]}/{v[1]}" for k, v in per_cat.items()},
        "refusal_precision": round(tp / len(got_ref), 3) if got_ref else None,
        "refusal_recall": round(tp / len(exp_ref), 3) if exp_ref else None,
        "latency_total_p50_s": round(stt.median(lat_total), 4), "latency_total_p95_s": round(q(lat_total, .95), 4),
        "latency_retrieval_p95_s": round(q(lat_ret, .95), 4), "index_chunks": n_chunks, "index_pages": n_pages,
        "answer_mode": "llm" if os.getenv("ANTHROPIC_API_KEY") else "extractive (no API key)",
        "llm_errors": sum(1 for x in res if x.get("llm_error")),
    }
    errs = [x["llm_error"] for x in res if x.get("llm_error")]
    if errs:
        print("first LLM error:", errs[0])
    print(json.dumps(summary, indent=1))
    (HERE / "results.json").write_text(json.dumps({"summary": summary, "cases": res}, indent=1))
    if a.baselines:
        b = baselines(con, cases)
        for x in b:
            c = next(c for c in cases if c["id"] == x["id"])
            if c.get("expect_status") in ("REFUSED", "NEEDS_INFO"):
                x["B0_declined"] = bool(DECLINE.search(x["B0"]))
                x["B1_declined"] = bool(DECLINE.search(x["B1"]))
        (HERE / "baseline_outputs.json").write_text(json.dumps(b, indent=1))
        exp = [x for x in b if "B0_declined" in x]
        print("baseline decline rate on should-refuse cases (keyword heuristic, verify by hand):",
              {"B0": f"{sum(x['B0_declined'] for x in exp)}/{len(exp)}", "B1": f"{sum(x['B1_declined'] for x in exp)}/{len(exp)}"})


if __name__ == "__main__":
    main()
