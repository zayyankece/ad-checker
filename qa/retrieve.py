"""QA-2: metadata filter + BM25 over section chunks."""
import re
import time

from adc.config import TOP_K
from adc.db import rows

STOP = set("a an the of to for in on is are does do did what which who how when where why this that these those it its be by with "
           "and or my our we i me any all from as at about apply applies affect affected directive directives ad ads".split())


def fts_query(q):
    toks = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]*", q) if t.lower() not in STOP and len(t) > 1]
    if not toks:
        return None
    return " OR ".join('"' + t.replace('"', "") + '"' for t in toks)


def search(con, q, doc_ids=None, airbus_only=False, k=TOP_K):
    t0 = time.perf_counter()
    fq = fts_query(q)
    if not fq:
        return [], 0.0
    where, args = ["chunks_fts MATCH ?"], [fq]
    if doc_ids:
        where.append(f"f.doc_id IN ({','.join('?' * len(doc_ids))})")
        args += list(doc_ids)
    if airbus_only:
        where.append("d.is_airbus=1")
    sql = f"""SELECT f.chunk_id, f.doc_id, c.section, c.page, c.text, -bm25(chunks_fts, 0, 0, 2.0, 1.0) AS score,
                     d.docket, d.subject, d.url, d.fr_citation
              FROM chunks_fts f JOIN chunks c ON c.chunk_id=f.chunk_id JOIN documents d ON d.doc_id=f.doc_id
              WHERE {' AND '.join(where)} ORDER BY bm25(chunks_fts, 0, 0, 2.0, 1.0) LIMIT ?"""
    res = rows(con, sql, args + [k])
    return res, time.perf_counter() - t0
