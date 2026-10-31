"""Add one new directive by Federal Register document number, docket number or URL (needs internet for new ones)."""
import json
import re

import requests

from adc.config import RAW
from adc.db import connect, one, rows
from rules.extract import extract_doc

from .chunk import chunk_doc
from .fetch import fetch_one
from .load import DOCNUM, doc_type, norm_make, superseded_ads
from .revisions import build as build_revisions

DOCKET = re.compile(r"FAA-\d{4}-\d{3,5}", re.I)


def find_existing(con, ref):
    ref = ref.strip()
    m = DOCNUM.search(ref) or re.search(r"(?<!FAA-)\b(20\d{2}-\d{4,5})\b", ref)
    if m:
        hit = one(con, "SELECT doc_id FROM documents WHERE doc_number=? OR doc_id=?", (m.group(1), m.group(1)))
        if hit:
            return hit["doc_id"]
    d = DOCKET.search(ref)
    if d:
        hit = one(con, "SELECT latest_doc_id FROM revisions WHERE docket=?", (d.group(0).upper(),))
        if hit:
            return hit["latest_doc_id"]
    return None


def add_document(con, ref, client=None):
    existing = find_existing(con, ref)
    if existing:
        return existing, "already tracked"
    m = DOCNUM.search(ref) or re.search(r"(?<!FAA-)\b(20\d{2}-\d{4,5})\b", ref)
    if not m:
        raise ValueError("Give a Federal Register document number (e.g. 2026-21334) or its URL. "
                         "Docket numbers can only be used for dockets already in the system.")
    doc_number = m.group(1)
    s = requests.Session()
    s.headers["User-Agent"] = "adc-hackathon/0.1"
    fetch_one(s, doc_number)
    meta = json.loads((RAW / f"{doc_number}.json").read_text())
    title = meta.get("title") or ""
    if "airworthiness directives" not in title.lower():
        raise ValueError(f"{doc_number} is not an airworthiness directive: {title!r}")
    summary = meta.get("abstract") or ""
    dk = DOCKET.search(" ".join(meta.get("docket_ids") or []))
    docket = dk.group(0).upper() if dk else doc_number
    make = norm_make(None, title)
    con.execute("INSERT OR REPLACE INTO documents (doc_id, row_idx, doc_number, docket, issue_date, publish_date, comments_due, "
                "make_raw, make_norm, is_airbus, product_type, subject, doc_type, final_ad_number, supersedes_ad, fr_citation, "
                "fr_page_start, fr_page_end, url, related_docs, summary, notes, text_source, embedded_text) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    doc_number, -1, doc_number, docket, meta.get("publication_date"), meta.get("publication_date"),
                    meta.get("comments_close_on"), make, make, int(bool(make and make.startswith("Airbus"))), "Aircraft", title,
                    doc_type(title, summary, ""), None, superseded_ads(summary), f"Federal Register {meta.get('start_page')}",
                    meta.get("start_page"), meta.get("end_page"), meta.get("html_url"), "[]", summary, "added in app",
                    "federal_register", None))
    d = one(con, "SELECT * FROM documents WHERE doc_id=?", (doc_number,))
    chunk_doc(con, d)
    con.commit()
    build_revisions()
    extract_doc(con, doc_number, client)
    return doc_number, "added"
