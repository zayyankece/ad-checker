import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

from adc.db import connect, one, rows
from adc.fleet import doc_status, get_rule, impact_report, load_fleet
from qa.pipeline import ask

st.set_page_config(page_title="Directive Impact Checker", layout="wide")
con = connect()
fleet, bad = load_fleet()

COLORS = {"APPLIES": "#c0392b", "NOT APPLICABLE": "#27ae60", "CANNOT DETERMINE": "#e67e22"}


def badge(label):
    return f"<span style='background:{COLORS.get(label, '#777')};color:white;padding:2px 8px;border-radius:4px;font-weight:600'>{label}</span>"


def show_citation(c):
    head = f"`{c.get('chunk_id')}` · {c.get('section', '')} · page {c.get('page', '?')}"
    with st.expander(f"{c.get('text') or ''}"[:140] or head):
        st.markdown(head)
        st.markdown(f"> {c.get('quote')}")
        if c.get("url"):
            st.markdown(f"[Federal Register]({c['url']}) · {c.get('fr_citation', '')}")
        full = one(con, "SELECT text FROM chunks WHERE chunk_id=?", (c.get("chunk_id"),))
        if full:
            st.code(full["text"][:3000], language=None, wrap_lines=True)


st.title("Directive Impact Checker")
st.caption("Proposed FAA airworthiness directives · LLM reads, rule engine decides, engineer approves")

tab_ask, tab_fleet, tab_rules = st.tabs(["Ask", "Fleet impact", "Rule review"])

with tab_ask:
    q = st.text_input("Question", placeholder="Does FAA-2026-11813 apply to 9V-ZME?")
    if q:
        r = ask(con, q, fleet)
        if r["status"] == "REFUSED":
            st.error(f"**Cannot answer ({r['refusal_code']})** — {r['message']}")
        elif r["status"] == "NEEDS_INFO":
            st.warning(f"**More information needed{' (' + r['refusal_code'] + ')' if r['refusal_code'] else ''}** — {r['message']}")
        elif r["message"]:
            st.info(r["message"])
        for w in dict.fromkeys(r["warnings"]):
            st.warning(w)
        for v in r["verdicts"]:
            if "history" in v:
                st.subheader(f"Docket {v['docket']}")
                st.dataframe(pd.DataFrame(v["history"])[["version_index", "doc_id", "doc_type", "issue_date", "is_latest", "docket_status", "supersedes_ad", "final_ad_number"]], hide_index=True)
            elif "directives" in v:
                st.subheader(v["registration"])
                st.dataframe(pd.DataFrame(v["directives"]), hide_index=True) if v["directives"] else st.write("No directive applies or is undetermined.")
            else:
                st.markdown(f"**{v['registration']}** ({v['model']}, MSN {v['msn']}) · `{v['doc_id']}` {badge(v['label'])}", unsafe_allow_html=True)
                for m in v.get("missing", []):
                    st.markdown(f"- ❓ {m}")
                for c in v.get("citations", []):
                    show_citation(c)
        if r["claims"]:
            st.subheader("Answer")
            for i, c in enumerate(r["claims"], 1):
                st.markdown(f"**[{i}]** {c.get('text')}")
                show_citation(c)
        if r.get("mode"):
            st.caption(f"Mode: {r['mode']}")
        st.caption(f"Latency: {json.dumps({k: round(v, 3) for k, v in r['latency'].items()})}")
        with st.expander("Debug: parsed query"):
            st.json(r.get("parsed", {}))

with tab_fleet:
    st.dataframe(pd.DataFrame(fleet), hide_index=True)
    if bad:
        st.error(f"Rejected fleet rows: {bad}")
    docs = rows(con, """SELECT d.doc_id, d.docket, d.issue_date, d.subject, r.docket_status, r.is_latest
                        FROM documents d JOIN revisions r USING(doc_id) JOIN rules USING(doc_id)
                        WHERE d.make_norm IN ('Airbus', 'Airbus Canada') ORDER BY d.issue_date DESC""")
    pick = st.selectbox("Directive", docs, format_func=lambda d: f"{d['docket']} · {d['issue_date']} · {d['subject'][:70]} · {d['docket_status']}{'' if d['is_latest'] else ' · OLD VERSION'}")
    if pick:
        rep = impact_report(con, pick["doc_id"], fleet)
        for w in rep["status"]["warnings"]:
            st.warning(w)
        st.caption(f"Rule status: {rep['rule_meta']['validation']} · extractor: {rep['rule_meta']['extractor']}"
                   + (f" · approved by {rep['rule_meta']['approved_by']}" if rep['rule_meta']['approved_by'] else " · NOT YET APPROVED"))
        counts = pd.Series([a["label"] for a in rep["aircraft"]]).value_counts().to_dict()
        st.markdown(" ".join(f"{badge(k)} × {v}" for k, v in counts.items()), unsafe_allow_html=True)
        for a in rep["aircraft"]:
            with st.expander(f"{a['registration']} · {a['model']} · MSN {a['msn']} → {a['label']}"):
                for e in a["evidence"]:
                    st.markdown(f"- **{e['result']}**: {e['reason']}  \n  > {e['quote']}")
                for m in a["missing"]:
                    st.markdown(f"- ❓ {m}")

with tab_rules:
    rl = rows(con, """SELECT r.doc_id, r.validation, r.extractor, r.approved_by, d.docket, d.subject FROM rules r
                      JOIN documents d USING(doc_id) ORDER BY r.validation, d.issue_date DESC""")
    st.dataframe(pd.DataFrame(rl)[["doc_id", "docket", "validation", "extractor", "approved_by"]], hide_index=True, height=220)
    pick = st.selectbox("Rule", rl, format_func=lambda r: f"{r['docket']} · {r['validation']} · {r['subject'][:60]}")
    if pick:
        rule, meta = get_rule(con, pick["doc_id"])
        from rules.extract import applicability_text
        cid, src = applicability_text(con, pick["doc_id"])
        c1, c2 = st.columns(2)
        with c1:
            st.markdown(f"**Source** `{cid}`")
            st.code(src[:4000], language=None, wrap_lines=True)
        with c2:
            st.markdown("**Extracted rule**")
            errs = json.loads(meta["validation_errors"] or "[]")
            for e in errs:
                st.error(e)
            edited = st.text_area("Rule JSON (editable)", rule.model_dump_json(indent=1), height=380)
            who = st.text_input("Engineer name")
            if st.button("Approve rule", disabled=not who):
                from rules.schema import Rule
                from rules.validate import validate
                new = Rule.model_validate_json(edited)
                errs = validate(new, src)
                if errs:
                    st.error(f"Not saved, validator failed: {errs}")
                else:
                    con.execute("UPDATE rules SET rule_json=?, validation='approved', validation_errors='[]', approved_by=?, approved_at=? WHERE doc_id=?",
                                (new.model_dump_json(), who, datetime.now().isoformat(timespec="seconds"), pick["doc_id"]))
                    con.commit()
                    st.success("Approved.")
