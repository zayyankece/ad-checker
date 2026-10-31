"""Find proposed Airbus airworthiness directives on the Federal Register that are not in the system yet, and add them."""
from datetime import date, timedelta

import requests

from adc.db import one

from .add import add_document

SEARCH = "https://www.federalregister.gov/api/v1/documents.json"


def discover(con, since=None, term="airworthiness directives airbus"):
    since = since or one(con, "SELECT MAX(publish_date) d FROM documents")["d"] or (date.today() - timedelta(days=60)).isoformat()
    params = [("conditions[term]", term), ("conditions[type][]", "PRORULE"),
              ("conditions[agencies][]", "federal-aviation-administration"),
              ("conditions[publication_date][gte]", since), ("order", "newest"), ("per_page", "50"),
              ("fields[]", "document_number"), ("fields[]", "title"), ("fields[]", "publication_date")]
    r = requests.get(SEARCH, params=params, timeout=30, headers={"User-Agent": "adc-hackathon/0.1"})
    r.raise_for_status()
    res = r.json().get("results", [])
    return [x for x in res if "airbus" in x.get("title", "").lower()
            and not one(con, "SELECT 1 FROM documents WHERE doc_number=?", (x["document_number"],))], len(res)


def discover_and_add(con, client=None):
    new, seen = discover(con)
    added = []
    for x in new:
        doc_id, how = add_document(con, x["document_number"], client)
        if how == "added":
            added.append(doc_id)
    return added, seen


if __name__ == "__main__":
    from adc.db import connect
    print(discover_and_add(connect()))
