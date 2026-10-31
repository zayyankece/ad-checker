"""ING-3/ING-4: split each document into section chunks with page numbers, build FTS5 index."""
import re

from adc.config import RAW
from adc.db import connect, rows

PREAMBLE = ["AGENCY", "ACTION", "SUMMARY", "DATES", "ADDRESSES", "FOR FURTHER INFORMATION CONTACT",
            "SUPPLEMENTARY INFORMATION"]
PRE_RE = re.compile(r"^\s*(" + "|".join(PREAMBLE) + r"):", re.M)
# Modern AD paragraph heading on its own line: "(c) Applicability"
PARA_RE = re.compile(r"^\s*\(([a-z])\)\s+([A-Z][A-Za-z0-9 ,/&()'-]{2,80})\s*$", re.M)
# Old-style AD: "Applicability: ..." / "Compliance: ..."
OLD_RE = re.compile(r"^\s*(Applicability|Compliance):", re.M)
# Federal Register body headings: short Title Case lines without a full stop
HEAD_RE = re.compile(r"^(?:[IVX]+\.\s+)?([A-Z][A-Za-z0-9 ,'()/&-]{3,80})\s*$", re.M)
KNOWN_HEADS = {"Background", "Comments", "FAA's Determination", "Proposed AD Requirements", "Costs of Compliance",
               "Authority for This Rulemaking", "Regulatory Findings", "The Proposed Amendment", "The Withdrawal",
               "Discussion", "Related Service Information", "Withdrawal of NPRM", "FAA's Conclusions",
               "Actions Since the NPRM Was Issued", "Comments Invited", "Related Service Information Under 1 CFR Part 51",
               "Differences Between This Proposed AD and the MCAI", "Explanation of Required Compliance Information",
               "Interim Action", "Applicability"}
PAGE_RE = re.compile(r"\[\[Page (\d+)\]\]")
MAX_CHARS = 2500


def page_at(text, pos, start_page):
    p = start_page
    for m in PAGE_RE.finditer(text, 0, pos):
        p = int(m.group(1))
    return p


def split_sections(text):
    """Return list of (section_label, start, end)."""
    cuts = []
    for m in PRE_RE.finditer(text):
        cuts.append((m.start(), m.group(1).title()))
    for m in PARA_RE.finditer(text):
        cuts.append((m.start(), f"({m.group(1)}) {m.group(2).strip()}"))
    for m in OLD_RE.finditer(text):
        cuts.append((m.start(), m.group(1)))
    for m in HEAD_RE.finditer(text):
        h = m.group(1).strip()
        if h in KNOWN_HEADS:
            cuts.append((m.start(), h))
    cuts.sort()
    dedup = []
    for pos, label in cuts:
        if dedup and pos - dedup[-1][0] < 3:
            continue
        dedup.append((pos, label))
    if not dedup or dedup[0][0] > 0:
        dedup.insert(0, (0, "Header"))
    out = []
    for i, (pos, label) in enumerate(dedup):
        end = dedup[i + 1][0] if i + 1 < len(dedup) else len(text)
        out.append((label, pos, end))
    return out


def slug(label):
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:40]


def clean(t):
    t = PAGE_RE.sub("", t)
    t = re.sub(r"[ \t]+", " ", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def windows(text, n=MAX_CHARS):
    if len(text) <= n:
        return [text]
    parts, cur = [], ""
    for para in re.split(r"\n\s*\n", text):
        if len(cur) + len(para) > n and cur:
            parts.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + para
    if cur:
        parts.append(cur)
    return parts


def source_text(doc):
    if doc["doc_number"]:
        p = RAW / f"{doc['doc_number']}.txt"
        if p.exists():
            return p.read_text(errors="ignore"), "federal_register"
    if doc["embedded_text"]:
        return doc["embedded_text"], "embedded"
    return None, "summary"


def chunk_doc(con, d):
    con.execute("DELETE FROM chunks WHERE doc_id=?", (d["doc_id"],))
    con.execute("DELETE FROM chunks_fts WHERE doc_id=?", (d["doc_id"],))
    out = [("Summary (dataset)", d["fr_page_start"], d["summary"] or "")]
    text, src = source_text(d)
    if text:
        for label, a, b in split_sections(text):
            page = page_at(text, a, d["fr_page_start"]) if src == "federal_register" else d["fr_page_start"]
            body = clean(text[a:b])
            if len(body) < 20:
                continue
            out.append((label, page, body))
    con.execute("UPDATE documents SET text_source=? WHERE doc_id=?", (src, d["doc_id"]))
    seen, n = {}, 0
    for ordn, (label, page, body) in enumerate(out):
        for k, w in enumerate(windows(body)):
            base = f"{d['doc_id']}#{slug(label)}"
            seen[base] = seen.get(base, 0) + 1
            cid = base if seen[base] == 1 else f"{base}-{seen[base]}"
            con.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?)", (cid, d["doc_id"], label, page, ordn * 100 + k, w))
            con.execute("INSERT INTO chunks_fts VALUES (?,?,?,?)", (cid, d["doc_id"], label, w))
            n += 1
    return n


def build():
    con = connect()
    con.execute("DELETE FROM chunks")
    con.execute("DELETE FROM chunks_fts")
    n = sum(chunk_doc(con, d) for d in rows(con, "SELECT * FROM documents"))
    con.commit()
    src = rows(con, "SELECT text_source, COUNT(*) n FROM documents GROUP BY 1")
    appl = con.execute("SELECT COUNT(DISTINCT doc_id) FROM chunks WHERE section LIKE '%Applicability%'").fetchone()[0]
    print(f"{n} chunks; sources {src}; docs with an Applicability chunk: {appl}")


if __name__ == "__main__":
    build()
