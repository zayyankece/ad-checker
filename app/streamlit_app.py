import json
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

from adc.actions import required_actions
from adc.db import connect, one, rows
from adc.fleet import (answer_question, delete_aircraft, get_rule, impact_report, load_fleet_db, sign_off, signoffs,
                       upsert_aircraft, version_diff)
from adc.inbox import build_inbox, card
from qa.pipeline import ask

st.set_page_config(page_title="Directive Impact Checker", page_icon="✈️", layout="wide")
con = connect()
fleet = load_fleet_db(con)
TODAY = date.today().isoformat()

FRIENDLY = {"APPLIES": "Affects", "NOT APPLICABLE": "Not affected", "CANNOT DETERMINE": "Need info"}
COLORS = {"Affects": "#c0392b", "Not affected": "#2e7d32", "Need info": "#e67e22", "Revised": "#1565c0",
          "Done": "#546e7a", "Proposed": "#6d4c41", "Withdrawn": "#546e7a", "Adopted": "#283593"}

st.markdown("""<style>
.chip{display:inline-block;color:#fff;padding:1px 9px;border-radius:10px;font-size:.78rem;font-weight:600;margin-right:6px}
.big{font-size:1.08rem;font-weight:600;margin:2px 0}
.muted{color:#6b7280;font-size:.88rem}
.quote{border-left:3px solid #9ca3af;padding:2px 10px;color:#374151;font-size:.9rem;margin:4px 0}
</style>""", unsafe_allow_html=True)


def chip(text, color=None):
    return f"<span class='chip' style='background:{color or COLORS.get(text, '#777')}'>{text}</span>"


def label(engine_label):
    return chip(FRIENDLY.get(engine_label, engine_label))


def due_text(due, days):
    if not due:
        return ""
    d = datetime.fromisoformat(due).strftime("%d %b %Y")
    if days is None:
        return f"Comments due {d}"
    return f"Comments due {d} ({days} days)" if days >= 0 else f"Comment period closed {d}"


def engineer():
    return st.session_state.get("engineer", "").strip()


def go(page, doc=None):
    st.session_state["goto"] = page
    st.session_state["open_doc"] = doc
    st.rerun()


# ---------- sidebar ----------
if "goto" in st.session_state:
    st.session_state["nav"] = st.session_state.pop("goto")
with st.sidebar:
    st.markdown("### ✈️ Directive Impact Checker")
    st.caption("Proposed FAA airworthiness directives vs. your fleet. The language model reads; rules decide; you sign off.")
    page = st.radio("Go to", ["Inbox", "Fleet", "Add directive", "Ask", "Expert"], key="nav", label_visibility="collapsed")
    st.text_input("Your name (for sign-off)", key="engineer")
    st.caption(f"Fleet: {len(fleet)} aircraft · today {TODAY}")


# ---------- shared pieces ----------
def render_question(reg, q, key):
    st.markdown(f"❓ **{q['text']}**")
    c1, c2, c3 = st.columns([1, 1, 4])
    if q["kind"] in ("yesno", "include"):
        yes, no = ("Yes", "No") if q["kind"] == "yesno" else ("Included", "Not included")
        if c1.button(yes, key=f"{key}-y"):
            answer_question(con, reg, q["key"], True)
            st.rerun()
        if c2.button(no, key=f"{key}-n"):
            answer_question(con, reg, q["key"], False)
            st.rerun()
    elif q["kind"] == "date":
        v = c3.date_input("Date", value=None, key=f"{key}-d", label_visibility="collapsed")
        if c1.button("Save", key=f"{key}-s", disabled=v is None):
            answer_question(con, reg, q["key"], v.isoformat())
            st.rerun()
    else:
        v = c3.text_input("Value", key=f"{key}-t", label_visibility="collapsed")
        if c1.button("Save", key=f"{key}-s", disabled=not v):
            answer_question(con, reg, q["key"], v.strip().upper())
            st.rerun()
    st.caption("Your answer is saved to this aircraft's record and reused by every directive that asks the same thing.")


def show_diffs(diffs):
    for df_ in diffs:
        st.markdown(f"**{df_['from']} ({df_['from_date']}) → {df_['to']} ({df_['to_date']})**")
        for c in df_["fleet_changes"]:
            st.markdown(f"{c['registration']}: {label(c['before'])} → {label(c['after'])}", unsafe_allow_html=True)
        if not df_["fleet_changes"]:
            st.markdown("Fleet impact unchanged.")
        if df_["applicability_changes"]:
            st.code("\n".join(df_["applicability_changes"]), language="diff", wrap_lines=True)


def render_detail(doc_id):
    if st.button("← Back to inbox"):
        go("Inbox")
    rep = impact_report(con, doc_id, fleet)
    d, rv = rep["status"]["doc"], rep["status"]["revision"]
    c = card(con, doc_id, fleet, TODAY) or {}
    status = {"WITHDRAWN": "Withdrawn", "ADOPTED": "Adopted"}.get(rv["docket_status"] if rv else "", "Proposed")
    st.markdown(f"## {d['docket']}")
    st.markdown(chip(status) + (chip("Revised") if c.get("revised") else "") +
                f"<span class='muted'>{due_text(d['comments_due'], c.get('days_left'))}</span>", unsafe_allow_html=True)
    if status == "Proposed":
        st.caption("Proposed rule (NPRM): not yet binding. Use this to plan work and decide whether to comment.")
    for w in rep["status"]["warnings"]:
        if not w.startswith("PROPOSED"):
            st.warning(w)
    if rep.get("error"):
        st.error(rep["error"])
        return
    cols = st.columns(2)
    cols[0].markdown(f"**Why it exists**  \n{(c.get('prompted') or '—')[:1].upper() + (c.get('prompted') or '—')[1:]}")
    req = c.get("require") or "—"
    extra = []
    if c.get("effort"):
        extra.append(f"~{c['effort']:g} work-hours per aircraft (FAA estimate)")
    if c.get("first_time"):
        extra.append(c["first_time"])
    cols[1].markdown(f"**What it would require**  \n{req[:1].upper() + req[1:]}" +
                     (f"  \n<span class='muted'>{' · '.join(extra)}</span>" if extra else ""), unsafe_allow_html=True)
    if c.get("revised"):
        r = c["revised"]
        lines = [f"**Revised {r['to']}** (previous version {r['from']})"]
        for fc in r["fleet_changes"]:
            lines.append(f"{fc['registration']}: {FRIENDLY.get(fc['before'])} → **{FRIENDLY.get(fc['after'])}**")
        for a, b in r["words"]:
            lines.append(f"Text changed: ~~{a}~~ → **{b}**")
        st.info("  \n".join(lines))

    st.markdown("### Your aircraft")
    so = signoffs(con, doc_id)
    relevant = [a for a in rep["aircraft"] if a["label"] != "NOT APPLICABLE"]
    clear = [a for a in rep["aircraft"] if a["label"] == "NOT APPLICABLE"]
    if not relevant:
        st.success(f"None of your {len(fleet)} aircraft are affected.")
    for a in relevant:
        with st.container(border=True):
            s_ = so.get(a["registration"])
            done = (f" <span class='muted'>· ✅ signed off by {s_['engineer']} "
                    f"({FRIENDLY.get(s_['final_label'], s_['final_label'])})</span>") if s_ else ""
            st.markdown(f"{label(a['label'])} <span class='big'>{a['registration']}</span> "
                        f"<span class='muted'>{a['model']} · MSN {a['msn']}</span>{done}", unsafe_allow_html=True)
            if a["label"] == "APPLIES":
                st.markdown(f"<span class='muted'>Because {a['reason']}.</span>", unsafe_allow_html=True)
            for i, q in enumerate(a["questions"]):
                render_question(a["registration"], q, f"q-{doc_id}-{a['registration']}-{i}")
    if clear:
        with st.expander(f"Not affected: {len(clear)} aircraft"):
            st.dataframe(pd.DataFrame([{"aircraft": a["registration"], "model": a["model"], "why not": a["reason"]} for a in clear]),
                         hide_index=True, use_container_width=True)

    open_q = sum(len(a["questions"]) for a in relevant)
    st.markdown("### Sign off")
    if open_q:
        st.warning(f"Answer the {open_q} open question(s) above first.")
    elif not engineer():
        st.info("Enter your name in the sidebar to sign off.")
    elif st.button(f"Confirm assessment for all {len(rep['aircraft'])} aircraft", type="primary"):
        for a in rep["aircraft"]:
            sign_off(con, doc_id, a["registration"], a["label"], "ACCEPT", a["label"], "", engineer())
        st.rerun()
    with st.expander("Override a result"):
        regs = [a["registration"] for a in rep["aircraft"]]
        r_ = st.selectbox("Aircraft", regs, key=f"ov-r-{doc_id}")
        new = st.selectbox("Correct result", ["APPLIES", "NOT APPLICABLE"], format_func=FRIENDLY.get, key=f"ov-l-{doc_id}")
        note = st.text_input("Reason (required)", key=f"ov-n-{doc_id}")
        if st.button("Save override", disabled=not (note and engineer()), key=f"ov-b-{doc_id}"):
            sys_label = next(a["label"] for a in rep["aircraft"] if a["registration"] == r_)
            sign_off(con, doc_id, r_, sys_label, "OVERRIDE", new, note, engineer())
            st.rerun()
    so = signoffs(con, doc_id)
    export = pd.DataFrame([{
        "docket": d["docket"], "document": doc_id, "aircraft": a["registration"], "model": a["model"], "msn": a["msn"],
        "system_result": FRIENDLY[a["label"]], "reason": a["reason"],
        "final_result": FRIENDLY.get(so.get(a["registration"], {}).get("final_label"), ""),
        "decision": so.get(a["registration"], {}).get("decision"), "engineer": so.get(a["registration"], {}).get("engineer"),
        "note": so.get(a["registration"], {}).get("note"), "comments_due": d["comments_due"], "source": d["url"],
    } for a in rep["aircraft"]])
    st.download_button("Download report (CSV)", export.to_csv(index=False), f"{d['docket']}_impact.csv", "text/csv")

    st.markdown("### Evidence")
    with st.expander("Source text the decision is based on"):
        from rules.extract import applicability_text
        cid, src = applicability_text(con, doc_id)
        quotes = {e["quote"] for a in rep["aircraft"] for e in a["evidence"] if e["quote"]}
        st.markdown(f"**Applicability** · `{cid}`" + (f" · [Federal Register]({d['url']})" if d["url"] else ""))
        st.code(src[:3000], language=None, wrap_lines=True)
        if quotes:
            st.markdown("Quoted by the rule:")
            for q in quotes:
                st.markdown(f"<div class='quote'>{q[:400]}</div>", unsafe_allow_html=True)
        meta = one(con, "SELECT validation, extractor, approved_by FROM rules WHERE doc_id=?", (doc_id,))
        st.caption(f"Rule: {meta['validation']} · extracted by {meta['extractor']}" +
                   (f" · approved by {meta['approved_by']}" if meta["approved_by"] else ""))
    act = required_actions(con, doc_id)
    if act["sections"]:
        with st.expander("Full required-actions text"):
            for s in act["sections"]:
                st.markdown(f"**{s['section']}** · page {s['page']} · `{s['chunk_id']}`")
                st.code(s["text"][:2500], language=None, wrap_lines=True)
    if rv and rv["n_versions"] > 1:
        with st.expander(f"Version history ({rv['n_versions']} versions)"):
            show_diffs(version_diff(con, doc_id, fleet))


def inbox_card(c):
    with st.container(border=True):
        top = []
        if c["done"]:
            top.append(chip("Done"))
        elif c["kind"] == "needs_input":
            top.append(chip("Need info"))
        elif c["kind"] == "affects":
            top.append(chip("Affects"))
        if c["revised"]:
            top.append(chip("Revised"))
        st.markdown(" ".join(top) + f" <b>{c['docket']}</b> <span class='muted'>· {due_text(c['due'], c['days_left'])}</span>",
                    unsafe_allow_html=True)
        if c["kind"] == "needs_input":
            head = f"Need your input on {', '.join(c['unknown'])}"
            if c["applies"]:
                head += f" · affects {', '.join(c['applies'])}"
        elif c["kind"] == "affects":
            head = f"Affects {', '.join(c['applies'])}"
        else:
            head = "Does not affect your fleet"
        st.markdown(f"<div class='big'>{head}</div>", unsafe_allow_html=True)
        bits = []
        if c["require"]:
            bits.append(f"Would require {c['require']}")
        if c["effort"]:
            bits.append(f"~{c['effort']:g} work-hours per aircraft")
        if bits:
            st.markdown(f"<span class='muted'>{' · '.join(bits)}</span>", unsafe_allow_html=True)
        if c["revised"] and (c["revised"]["fleet_changes"] or c["revised"]["words"]):
            ch = [f"{x['registration']} now {({'APPLIES': 'affected', 'NOT APPLICABLE': 'not affected'}).get(x['after'], 'needs info')}" for x in c["revised"]["fleet_changes"]]
            ch += [f"“{a}” → “{b}”" for a, b in c["revised"]["words"][:1]]
            st.markdown(f"<span class='muted'>What changed ({c['revised']['to']}): {'; '.join(ch)}</span>", unsafe_allow_html=True)
        if st.button("Open", key=f"open-{c['doc_id']}"):
            go("Inbox", c["doc_id"])


# ---------- pages ----------
if page == "Inbox":
    if st.session_state.get("open_doc"):
        render_detail(st.session_state["open_doc"])
    else:
        open_, older = build_inbox(con, fleet, TODAY)
        todo = [c for c in open_ if c["kind"] != "clear" and not c["done"]]
        done = [c for c in open_ if c["done"]]
        clear = [c for c in open_ if c["kind"] == "clear" and not c["done"]]
        st.markdown("## Inbox")
        n_in = sum(1 for c in todo if c["kind"] == "needs_input")
        n_af = sum(1 for c in todo if c["kind"] == "affects")
        st.markdown(f"<span class='muted'>Open proposals (comment period still open): "
                    f"<b>{n_in}</b> need your input · <b>{n_af}</b> affect your fleet · {len(clear)} don't affect you · "
                    f"{len(done)} done</span>", unsafe_allow_html=True)
        if not todo:
            st.success("Nothing to review.")
        for c in todo:
            inbox_card(c)
        if done:
            with st.expander(f"Done ({len(done)})"):
                for c in done:
                    inbox_card(c)
        with st.expander(f"Open proposals that don't affect your fleet ({len(clear)})"):
            for c in clear:
                cols = st.columns([5, 1])
                cols[0].markdown(f"**{c['docket']}** · {due_text(c['due'], c['days_left'])} · {c['require'] or ''}")
                if cols[1].button("Open", key=f"open-clear-{c['doc_id']}"):
                    go("Inbox", c["doc_id"])
        with st.expander(f"Older proposals, comment period closed ({len(older)})"):
            for c in older:
                tag = ("affects " + ", ".join(c["applies"])) if c["applies"] else ("need info" if c["unknown"] else "not affected")
                cols = st.columns([5, 1])
                cols[0].markdown(f"**{c['docket']}** · {c['due'] or 'no date'} · {tag}")
                if cols[1].button("Open", key=f"open-old-{c['doc_id']}"):
                    go("Inbox", c["doc_id"])

elif page == "Fleet":
    st.markdown("## Fleet")
    st.caption("Edit cells directly, add a row at the bottom, or select rows and delete them. Click Save when done. "
               "Only registration and model are required; anything missing turns into a question in the inbox when a directive needs it.")
    known_models = sorted({str(v) for r in rows(con, "SELECT rule_json FROM rules")
                           for g in json.loads(r["rule_json"])["groups"] for c in g["conditions"]
                           if c["field"] == "model" for v in c["values"]} | {a["model"] for a in fleet})
    df = pd.DataFrame([{"registration": a["registration"], "model": a["model"], "msn": a["msn"],
                        "cert_date": date.fromisoformat(a["cert_date"]) if a["cert_date"] else None,
                        "engine_model": a["engine_model"]} for a in fleet])
    edited = st.data_editor(df, num_rows="dynamic", use_container_width=True, key="fleet-editor", column_config={
        "registration": st.column_config.TextColumn("Registration", required=True),
        "model": st.column_config.SelectboxColumn("Model", options=known_models, required=True),
        "msn": st.column_config.TextColumn("MSN"),
        "cert_date": st.column_config.DateColumn("Original airworthiness cert."),
        "engine_model": st.column_config.TextColumn("Engine"),
    })
    if st.button("Save fleet", type="primary"):
        keep = set()
        for _, r in edited.dropna(subset=["registration", "model"]).iterrows():
            reg = str(r["registration"]).strip().upper()
            keep.add(reg)
            old = next((a for a in fleet if a["registration"] == reg), {})
            cd = r["cert_date"]
            upsert_aircraft(con, {"registration": reg, "model": r["model"], "msn": r["msn"] if pd.notna(r["msn"]) else None,
                                  "cert_date": cd.isoformat() if cd is not None and pd.notna(cd) else None,
                                  "engine_model": r["engine_model"] if pd.notna(r["engine_model"]) else None,
                                  "mods_installed": old.get("mods_installed"), "parts_installed": old.get("parts_installed"),
                                  "facts": old.get("facts")})
        for a in fleet:
            if a["registration"] not in keep:
                delete_aircraft(con, a["registration"])
        st.rerun()
    up = st.file_uploader("Or import a CSV (registration, model, msn, cert_date, engine_model)", type="csv")
    if up and st.button("Import CSV"):
        for _, r in pd.read_csv(up, dtype=str).fillna("").iterrows():
            if r.get("registration") and r.get("model"):
                upsert_aircraft(con, {"registration": r["registration"], "model": r["model"], "msn": r.get("msn") or None,
                                      "cert_date": r.get("cert_date") or None, "engine_model": r.get("engine_model") or None})
        st.rerun()

    st.markdown("### Impact per aircraft (open proposals)")
    open_, _ = build_inbox(con, fleet, TODAY)
    st.dataframe(pd.DataFrame([{
        "aircraft": a["registration"], "model": a["model"],
        "affected by": ", ".join(c["docket"] for c in open_ if a["registration"] in c["applies"]) or "—",
        "need info": ", ".join(c["docket"] for c in open_ if a["registration"] in c["unknown"]) or "—",
        "answers on file": len(a["facts"])} for a in fleet]), hide_index=True, use_container_width=True)
    with st.expander("Answers on file (from inbox questions)"):
        any_ = False
        for a in fleet:
            for k, v in a["facts"].items():
                any_ = True
                cols = st.columns([2, 5, 1, 1])
                cols[0].markdown(a["registration"])
                cols[1].markdown(f"`{k}`")
                cols[2].markdown("Yes" if v is True else "No" if v is False else str(v))
                if cols[3].button("Clear", key=f"clr-{a['registration']}-{k}"):
                    f = dict(a["facts"])
                    f.pop(k)
                    con.execute("UPDATE aircraft SET facts=? WHERE registration=?", (json.dumps(f), a["registration"]))
                    con.commit()
                    st.rerun()
        if not any_:
            st.write("None yet.")

elif page == "Add directive":
    st.markdown("## Add a new directive")
    st.caption("Paste the Federal Register document number or link of a new proposed airworthiness directive. "
               "The system downloads the full text, reads the applicability section, checks your fleet and opens the result. "
               "Aircraft it cannot decide on show up as “Need info” with a question for you.")
    ref = st.text_input("Document number or link", placeholder="e.g. 2026-21334 or https://www.federalregister.gov/documents/...")
    if st.button("Add", type="primary", disabled=not ref):
        from adc.llm import client
        from ingest.add import add_document
        try:
            with st.spinner("Downloading and reading…"):
                doc_id, how = add_document(con, ref, client())
            go("Inbox", doc_id)
        except Exception as e:
            st.error(f"Could not add: {e}")

elif page == "Ask":
    st.markdown("## Ask")
    st.caption("Answers come only from the directive texts, with citations. Applicability is decided by the rule engine.")
    q = st.text_input("Question", placeholder="Does FAA-2026-11813 apply to 9V-ZMF?")
    if q:
        r = ask(con, q, fleet)
        if r["status"] == "REFUSED":
            st.error(f"**Can't answer** ({r['refusal_code']}): {r['message']}")
        elif r["status"] == "NEEDS_INFO" and r["refusal_code"]:
            st.warning(f"**Need more information**: {r['message']}")
        for w in dict.fromkeys(r["warnings"]):
            st.caption(f"⚠️ {w}")
        for i, v in enumerate(r["verdicts"]):
            if "history" in v:
                st.markdown(f"**Docket {v['docket']}**: {len(v['history'])} versions")
                st.dataframe(pd.DataFrame(v["history"])[["version_index", "doc_id", "doc_type", "issue_date", "docket_status"]], hide_index=True)
                show_diffs(v.get("diffs", []))
            elif "directives" in v:
                st.markdown(f"**{v['registration']}**")
                if v["directives"]:
                    st.dataframe(pd.DataFrame(v["directives"])[["docket", "label", "comments_due"]], hide_index=True)
                else:
                    st.write("Nothing applies.")
            else:
                st.markdown(f"{label(v['label'])} **{v['registration']}** · {v.get('doc_id', '')} "
                            f"<span class='muted'>{v.get('reason', '')}</span>", unsafe_allow_html=True)
                for qq in v.get("questions", []):
                    st.markdown(f"❓ {qq['text']}")
                if v.get("doc_id") and st.button("Open directive", key=f"ask-open-{i}"):
                    go("Inbox", v["doc_id"])
        for i, c in enumerate(r["claims"], 1):
            st.markdown(f"**[{i}]** {c.get('text')}")
            st.markdown(f"<div class='quote'>{c.get('quote')}</div><span class='muted'>{c.get('docket', '')} · "
                        f"{c.get('section', '')} · page {c.get('page', '?')}</span>", unsafe_allow_html=True)
        st.caption(f"Answered in {r['latency'].get('total_s', 0):.2f}s" + (f" · {r['mode']}" if r.get("mode") else ""))

elif page == "Expert":
    st.markdown("## Expert")
    st.caption("Rule review and raw data. Day-to-day use does not need this page.")
    rl = rows(con, """SELECT r.doc_id, r.validation, r.extractor, r.approved_by, d.docket FROM rules r
                      JOIN documents d USING(doc_id) WHERE d.make_norm IN ('Airbus','Airbus Canada')
                      ORDER BY r.validation, d.issue_date DESC""")
    pick = st.selectbox("Rule", rl, format_func=lambda r: f"{r['docket']} · {r['validation']} · {r['doc_id']}")
    if pick:
        rule, meta = get_rule(con, pick["doc_id"])
        from rules.extract import applicability_text
        cid, src = applicability_text(con, pick["doc_id"])
        c1, c2 = st.columns(2)
        c1.markdown(f"**Source** `{cid}`")
        c1.code(src[:4000], language=None, wrap_lines=True)
        c2.markdown("**Extracted rule**")
        for e in json.loads(meta["validation_errors"] or "[]"):
            c2.error(e)
        edited = c2.text_area("Rule JSON (editable)", rule.model_dump_json(indent=1), height=360)
        if c2.button("Approve rule", disabled=not engineer()):
            from rules.schema import Rule
            from rules.validate import validate
            new = Rule.model_validate_json(edited)
            errs = validate(new, src)
            if errs:
                c2.error(f"Not saved, validator failed: {errs}")
            else:
                con.execute("UPDATE rules SET rule_json=?, validation='approved', validation_errors='[]', approved_by=?, approved_at=? "
                            "WHERE doc_id=?", (new.model_dump_json(), engineer(), datetime.now().isoformat(timespec="seconds"), pick["doc_id"]))
                con.commit()
                c2.success("Approved.")
