import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

from adc.db import connect, one, rows
from adc.actions import required_actions
from adc.fleet import doc_status, get_rule, impact_report, load_fleet, review_queue, sign_off, signoffs, version_diff
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


def show_diffs(diffs):
    for df_ in diffs:
        st.markdown(f"**Changes {df_['from']} ({df_['from_date']}) → {df_['to']} ({df_['to_date']})**")
        if df_["fleet_changes"]:
            for c in df_["fleet_changes"]:
                st.markdown(f"- **{c['registration']}**: {c['before']} → {badge(c['after'] or 'n/a')}", unsafe_allow_html=True)
        else:
            st.markdown("- Fleet impact unchanged")
        if df_["applicability_changes"]:
            st.code("\n".join(df_["applicability_changes"]), language="diff", wrap_lines=True)
        if df_["sections_added"] or df_["sections_removed"]:
            st.caption(f"Sections added: {df_['sections_added']} · removed: {df_['sections_removed']}")


def render_directive(doc_id, key):
    rep = impact_report(con, doc_id, fleet)
    d = rep["status"]["doc"]
    st.markdown(f"### {d['docket']} · `{doc_id}`")
    st.caption((d["summary"] or "")[:400])
    for w in rep["status"]["warnings"]:
        st.warning(w)
    if d["comments_due"]:
        st.markdown(f"**Comment deadline:** {d['comments_due']} · [Federal Register]({d['url']})" if d["url"] else f"**Comment deadline:** {d['comments_due']}")
    if rep["status"]["revision"] and rep["status"]["revision"]["n_versions"] > 1:
        with st.expander(f"Version history ({rep['status']['revision']['n_versions']} versions) and what changed", expanded=True):
            show_diffs(version_diff(con, doc_id, fleet))
    if rep.get("error"):
        st.error(rep["error"])
        return
    st.caption(f"Rule status: {rep['rule_meta']['validation']} · extractor: {rep['rule_meta']['extractor']}"
               + (f" · rule approved by {rep['rule_meta']['approved_by']}" if rep['rule_meta']['approved_by'] else " · rule not yet approved"))

    act = required_actions(con, doc_id)
    with st.expander("What the proposal would require (from the regulatory text)", expanded=False):
        if act["effort"]:
            st.markdown(f"**Estimated effort (FAA cost section):** up to {act['effort']['max_work_hours']:g} work-hours per aircraft · `{act['effort']['chunk_id']}`")
        if act["compliance_times"]:
            st.markdown("**Compliance times found:**")
            for t in act["compliance_times"]:
                st.markdown(f"- {t['text']} · `{t['chunk_id']}`")
        for c in act["sections"]:
            st.markdown(f"**{c['section']}** · page {c['page']} · `{c['chunk_id']}`")
            st.code(c["text"][:2500], language=None, wrap_lines=True)
        if not (act["sections"] or act["compliance_times"]):
            st.write("No required-action section found in the available text.")

    counts = pd.Series([a["label"] for a in rep["aircraft"]]).value_counts().to_dict()
    st.markdown(" ".join(f"{badge(k)} × {v}" for k, v in counts.items()), unsafe_allow_html=True)
    so = signoffs(con, doc_id)
    engineer = st.text_input("Engineer name (for sign-off)", key=f"{key}-eng-{doc_id}")
    for a in rep["aircraft"]:
        s_ = so.get(a["registration"])
        tag = f" · ✅ signed off by {s_['engineer']}: {s_['final_label']}" if s_ else ""
        with st.expander(f"{a['registration']} · {a['model']} · MSN {a['msn']} → {a['label']}{tag}"):
            for e in a["evidence"]:
                st.markdown(f"- **{e['result']}**: {e['reason']}  \n  > {e['quote']}")
            for m in a["missing"]:
                st.markdown(f"- ❓ {m}")
            c1, c2, c3 = st.columns([1, 1, 2])
            note = c3.text_input("Note", key=f"{key}-note-{doc_id}-{a['registration']}", value=s_["note"] if s_ else "")
            if c1.button("Accept", key=f"{key}-ok-{doc_id}-{a['registration']}", disabled=not engineer):
                sign_off(con, doc_id, a["registration"], a["label"], "ACCEPT", a["label"], note, engineer)
                st.rerun()
            override = c2.selectbox("Override to", ["", "APPLIES", "NOT APPLICABLE"], key=f"{key}-ov-{doc_id}-{a['registration']}")
            if override and c2.button("Save override", key=f"{key}-ovb-{doc_id}-{a['registration']}", disabled=not (engineer and note)):
                sign_off(con, doc_id, a["registration"], a["label"], "OVERRIDE", override, note, engineer)
                st.rerun()
            if override and not note:
                c2.caption("Override needs a note.")
    so = signoffs(con, doc_id)
    export = pd.DataFrame([{
        "docket": d["docket"], "doc_id": doc_id, "registration": a["registration"], "model": a["model"], "msn": a["msn"],
        "system_label": a["label"], "final_label": so.get(a["registration"], {}).get("final_label"),
        "decision": so.get(a["registration"], {}).get("decision"), "engineer": so.get(a["registration"], {}).get("engineer"),
        "note": so.get(a["registration"], {}).get("note"), "missing_info": " | ".join(a["missing"]),
        "evidence": " | ".join(f"{e['reason']}: {e['quote']}" for e in a["evidence"] if e["quote"]),
        "status": " | ".join(rep["status"]["warnings"]), "comments_due": d["comments_due"],
    } for a in rep["aircraft"]])
    st.download_button("Export impact report (CSV)", export.to_csv(index=False), file_name=f"impact_{d['docket']}.csv",
                       mime="text/csv", key=f"{key}-dl-{doc_id}")


st.title("Directive Impact Checker")
st.caption("Proposed FAA airworthiness directives · LLM reads, rule engine decides, engineer approves")

tab_queue, tab_ask, tab_fleet, tab_rules = st.tabs(["Review queue", "Ask", "Fleet impact", "Rule review"])

with tab_queue:
    today = datetime.now().date().isoformat()
    q_ = review_queue(con, fleet, today)
    open_ = [x for x in q_ if x["days_left"] is not None and x["days_left"] >= 0]
    st.markdown(f"**{len(open_)} open proposals** affect or may affect the fleet (comment period still open, as of {today}). "
                f"{len(q_) - len(open_)} more with closed comment periods.")
    show_closed = st.checkbox("Show proposals with closed comment period")
    view = q_ if show_closed else open_
    if view:
        st.dataframe(pd.DataFrame(view)[["docket", "comments_due", "days_left", "applies", "cannot_determine", "signed_off", "status", "subject"]],
                     hide_index=True, use_container_width=True)
        sel = st.selectbox("Open", view, format_func=lambda x: f"{x['docket']} · due {x['comments_due']} ({x['days_left']} days) · applies: {x['applies'] or '-'}")
        if sel:
            render_directive(sel["doc_id"], "queue")

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
                show_diffs(v.get("diffs", []))
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
        render_directive(pick["doc_id"], "fleet")

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
