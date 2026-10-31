"""ING-2: fetch full text from the Federal Register API. Run on a machine with internet access.

python -m ingest.fetch            # Airbus only
python -m ingest.fetch --all      # every row with a document number
"""
import argparse
import json
import re
import time

import requests

from adc.config import RAW
from adc.db import connect, rows

API = "https://www.federalregister.gov/api/v1/documents/{}.json"
FIELDS = ["document_number", "title", "type", "action", "publication_date", "start_page", "end_page",
          "docket_ids", "raw_text_url", "html_url", "comments_close_on", "dates"]


def fetch_one(s, doc_number):
    meta_p = RAW / f"{doc_number}.json"
    text_p = RAW / f"{doc_number}.txt"
    if text_p.exists() and text_p.stat().st_size > 500:
        return "cached"
    r = s.get(API.format(doc_number), params=[("fields[]", f) for f in FIELDS], timeout=30)
    r.raise_for_status()
    meta = r.json()
    meta_p.write_text(json.dumps(meta, indent=1))
    t = s.get(meta["raw_text_url"], timeout=60)
    t.raise_for_status()
    body = t.text
    m = re.search(r"<pre>(.*)</pre>", body, re.S)
    if m:
        body = m.group(1)
    body = re.sub(r"<[^>]+>", "", body)
    body = body.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&#x2014;", "-")
    text_p.write_text(body)
    return "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    RAW.mkdir(parents=True, exist_ok=True)
    con = connect()
    where = "doc_number IS NOT NULL" + ("" if a.all else " AND is_airbus=1")
    docs = rows(con, f"SELECT doc_number FROM documents WHERE {where}")
    s = requests.Session()
    s.headers["User-Agent"] = "adc-hackathon/0.1"
    errors = []
    for i, d in enumerate(docs, 1):
        try:
            st = fetch_one(s, d["doc_number"])
            if st == "ok":
                time.sleep(0.3)
        except Exception as e:
            st = f"ERROR {e}"
            errors.append((d["doc_number"], str(e)))
        print(f"[{i}/{len(docs)}] {d['doc_number']} {st}")
    (RAW.parent / "fetch_errors.csv").write_text("doc_number,error\n" + "\n".join(f"{a},{b!r}" for a, b in errors))
    print(f"done, {len(errors)} errors")


if __name__ == "__main__":
    main()
