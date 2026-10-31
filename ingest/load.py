"""ING-1: load xlsx, normalize make, extract Federal Register document numbers, derive doc type."""
import re
from datetime import datetime

import pandas as pd

from adc.config import XLSX
from adc.db import connect, dumps

MAKE_ALIASES = [
    (r"airbus helicopters deutschland|eurocopter deutschland|\(ahd\)", "Airbus Helicopters Deutschland"),
    (r"airbus helicopters|eurocopter", "Airbus Helicopters"),
    (r"airbus canada|c series", "Airbus Canada"),
    (r"airbus defence", "Airbus Defence and Space"),
    (r"airbus", "Airbus"),
    (r"boeing", "Boeing"),
    (r"mcdonnell douglas", "McDonnell Douglas"),
    (r"bombardier", "Bombardier"),
    (r"embraer", "Embraer"),
]

DOCNUM = re.compile(r"(?:documents/\d{4}/\d{2}/\d{2}/|/d/|/a/)(?:R1-)?(\d{4}-\d{4,5})")
SUPERSEDE = re.compile(r"supersede\s+(?:airworthiness directives?\s*\(ADs?\)\s*)?((?:(?:AD\s+)?\d{2,4}-\s?\d{2}-\s?\d{2}(?:R\d)?(?:,\s*|\s+and\s+|\s*)?)+)", re.I)
ADNUM = re.compile(r"(\d{2,4})-\s?(\d{2})-\s?(\d{2}(?:R\d)?)")


def superseded_ads(summary):
    m = SUPERSEDE.search(summary or "")
    if not m:
        return None
    return ",".join("-".join(g) for g in ADNUM.findall(m.group(1))) or None


def norm_make(make, subject):
    make = make if isinstance(make, str) else ""
    s = f"{make} {subject or ''}".lower()
    if "|" in str(make or ""):
        s = str(subject).lower()
    for pat, name in MAKE_ALIASES:
        if re.search(pat, s):
            return name
    return (make or "").strip() or None


def iso(d):
    if pd.isna(d) or not str(d).strip():
        return None
    return datetime.strptime(str(d).strip(), "%m/%d/%Y").date().isoformat()


def pages(p):
    m = re.findall(r"\d+", str(p))
    if not m:
        return None, None
    return int(m[0]), int(m[1]) if len(m) > 1 else int(m[0])


def parse_link(field):
    """Return (url, [(doc_number, role)], embedded_text)."""
    if not isinstance(field, str) or not field.strip():
        return None, [], None
    f = field.strip()
    if not f.startswith("http"):
        return None, [], f
    docs = []
    for m in DOCNUM.finditer(f):
        before = f[: m.start()].lower()
        role = "primary"
        tail = before[-40:]
        if "withdrawal" in tail:
            role = "withdrawal"
        elif "correction" in tail:
            role = "correction"
        docs.append((m.group(1), role))
    url = re.split(r'["\s]', f)[0]
    return url, docs, None


def doc_type(subject, summary, comments):
    s = f"{subject} {summary}".lower()
    if "withdrawal" in str(subject).lower() or "is withdrawing" in s or "is withdrawn" in s:
        return "WITHDRAWAL"
    if "supplemental notice of proposed rulemaking" in s or "snprm" in s or "reopening of the comment period" in s:
        return "SNPRM"
    return "NPRM"


def load():
    df = pd.read_excel(XLSX, dtype=str).astype(object).where(lambda d: d.notna(), None)
    con = connect()
    con.execute("DELETE FROM documents")
    for i, r in df.iterrows():
        url, docs, embedded = parse_link(r["Link for Submitting Public Comment"])
        primary = next((d for d, role in docs if role == "primary"), None)
        docket = r["Docket Number"].strip()
        issue = iso(r["Issue Date"])
        doc_id = primary or f"{docket}@{issue}"
        ps, pe = pages(r["Page Number"])
        make_norm = norm_make(r["Make"], r["Subject Heading"])
        summary = r["Summary"] if isinstance(r["Summary"], str) else ""
        sup = superseded_ads(summary)
        dtype = doc_type(r["Subject Heading"], summary, r["Comments"])
        withdrawal_doc = next((d for d, role in docs if role == "withdrawal"), None)
        if dtype == "WITHDRAWAL" and withdrawal_doc:
            primary = withdrawal_doc
        doc_id = primary or f"{docket}@{issue}"
        if con.execute("SELECT 1 FROM documents WHERE doc_id=?", (doc_id,)).fetchone():
            doc_id = f"{doc_id}@{issue}"
        con.execute(
            "INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                doc_id, i, primary, docket, issue, iso(r["Citation Publish Date"]), iso(r["Comments Due Date"]),
                r["Make"], make_norm, int(bool(make_norm and make_norm.startswith("Airbus"))),
                r["Product Type"], r["Subject Heading"], dtype,
                r["AD Number"] if isinstance(r["AD Number"], str) else None,
                sup, r["Citation"], ps, pe, url, dumps(docs), summary,
                r["Comments"] if isinstance(r["Comments"], str) else None,
                "embedded" if embedded else "summary", embedded,
            ),
        )
    con.commit()
    n = con.execute("SELECT COUNT(*), SUM(is_airbus), SUM(doc_number IS NOT NULL) FROM documents").fetchone()
    print(f"loaded {n[0]} docs, {n[1]} Airbus, {n[2]} with Federal Register doc number")


if __name__ == "__main__":
    load()
