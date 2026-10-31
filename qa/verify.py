"""QA-5: every claim must carry a quote that exists verbatim in the chunk it cites."""
from rules.validate import norm


def verify(claims, chunks):
    by_id = {c["chunk_id"]: norm(c["text"]) for c in chunks}
    errors = []
    if not claims:
        return ["no well-formed claims"]
    for i, cl in enumerate(claims):
        if not isinstance(cl, dict):
            errors.append(f"claim {i}: malformed")
            continue
        cid, q = cl.get("chunk_id"), norm(cl.get("quote", ""))
        if cid not in by_id:
            errors.append(f"claim {i}: cites unknown chunk {cid}")
        elif len(q) < 8 or q not in by_id[cid]:
            errors.append(f"claim {i}: quote not found in {cid}")
    return errors
