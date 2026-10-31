"""APP-2: every quote must be verbatim in the source; every value must be supported by its quote."""
import re

from .schema import Rule


def norm(s):
    s = str(s).replace("–", "-").replace("—", "-").replace(" ", " ")
    s = re.sub(r"-\s+", "-", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def value_supported(v, quote_n):
    v = norm(v)
    if v in quote_n:
        return True
    # "A320-212" supported by "A320-211, -212" style abbreviation
    m = re.match(r"^([a-z]*\d{3}[a-z]?)(-.+)$", v)
    if m and m.group(1) in quote_n and m.group(2) in quote_n:
        return True
    # "A300 B4-603" supported by "Model A300 B4-601, B4-603"
    m = re.match(r"^([a-z]*\d{3}) (\S+)$", v)
    if m and m.group(1) in quote_n and m.group(2) in quote_n:
        return True
    # ISO date supported by "October 1, 2024"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        from datetime import date
        d = date.fromisoformat(v)
        if f"{d.strftime('%B').lower()} {d.day}, {d.year}" in quote_n:
            return True
    # numbers written with thousands separator
    if v.isdigit() and f"{int(v):,}" in quote_n:
        return True
    return False


def validate(rule: Rule, source: str):
    src = norm(source)
    errs = []
    conds = [c for g in rule.groups for c in g.conditions] + list(rule.exclusions)
    for c in conds:
        q = norm(c.quote)
        if not q or q not in src:
            errs.append(f"quote not found in source: {c.quote[:80]!r}")
            continue
        for v in c.values:
            if not value_supported(v, q):
                errs.append(f"value {v!r} not supported by its quote")
        for v in (c.min, c.max):
            if v is not None and not value_supported(v, q):
                errs.append(f"range bound {v} not supported by its quote")
    if not rule.groups:
        errs.append("rule has no condition groups")
    return errs


def coverage_gaps(rule: Rule, source: str):
    """Omission check: every aircraft model and MSN written in the applicability text must be used somewhere in the rule
    (scope or exclusions). The verbatim validator catches invented values; this catches dropped ones."""
    from .engine import family, norm_model
    from .extract import EXCEPT, parse_models
    scan = re.sub(r"\s+", " ", re.sub(r"-\s+(?=\w)", "-", source))
    scan = re.sub(r"^\s*(?:\([a-z]\)\s*)?Applicability:?\s*", "", scan)
    ex = EXCEPT.search(scan)
    incl_models, _, last_fam = parse_models(scan[:ex.start()] if ex else scan)
    excl_models = parse_models(scan[ex.start():], last_fam)[0] if ex else []
    conds = [c for g in rule.groups for c in g.conditions] + list(rule.exclusions)
    used = {norm_model(v) for c in conds if c.field == "model" for v in c.values}
    fams = {norm_model(v) for c in conds if c.field == "model_family" for v in c.values}
    gaps = [m for m in dict.fromkeys(incl_models + excl_models) if norm_model(m) not in used and family(m) not in fams]
    msns = set()
    for m in re.finditer(r"(?:manufacturer(?:'s)? serial numbers?|MSNs?)\s*\(?(?:MSN)?\)?\s*((?:\d{2,6}(?:\s*(?:,|and|or|through|to)\s*)?)+)", scan, re.I):
        msns |= set(re.findall(r"\d{2,6}", m.group(1)))
    used_msn = {str(v) for c in conds if c.field == "msn" for v in list(c.values) + [c.min, c.max] if v is not None}
    gaps += [f"MSN {n}" for n in sorted(msns - used_msn)]
    return gaps
