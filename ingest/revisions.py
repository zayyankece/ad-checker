"""REV-1..3: version chain per docket, latest version, docket status."""
import re
from adc.db import connect, rows


def build():
    con = connect()
    con.execute("DELETE FROM revisions")
    docs = rows(con, "SELECT doc_id, docket, issue_date, publish_date, doc_type, final_ad_number FROM documents")
    by = {}
    for d in docs:
        key = re.sub(r"R\d$", "", d["docket"])
        by.setdefault(key, []).append(d)
    for key, vs in by.items():
        vs.sort(key=lambda d: (d["publish_date"] or d["issue_date"] or "", d["issue_date"] or ""))
        latest = vs[-1]
        if latest["doc_type"] == "WITHDRAWAL":
            status = "WITHDRAWN"
        elif any(v["final_ad_number"] for v in vs):
            status = "ADOPTED"
        else:
            status = "ACTIVE_PROPOSAL"
        for i, v in enumerate(vs, 1):
            con.execute("INSERT INTO revisions VALUES (?,?,?,?,?,?,?)",
                        (v["doc_id"], key, i, len(vs), int(v is latest), latest["doc_id"], status))
    con.commit()
    s = rows(con, "SELECT docket_status, COUNT(DISTINCT docket) n FROM revisions GROUP BY 1")
    m = con.execute("SELECT COUNT(DISTINCT docket) FROM revisions WHERE n_versions>1").fetchone()[0]
    print(f"dockets by status {s}; multi-version dockets: {m}")


def history(con, doc_id):
    r = con.execute("SELECT docket FROM revisions WHERE doc_id=?", (doc_id,)).fetchone()
    if not r:
        return []
    return rows(con, """SELECT r.*, d.doc_type, d.issue_date, d.publish_date, d.final_ad_number, d.url, d.supersedes_ad, d.comments_due
                        FROM revisions r JOIN documents d USING(doc_id) WHERE r.docket=? ORDER BY version_index""", (r[0],))


if __name__ == "__main__":
    build()
