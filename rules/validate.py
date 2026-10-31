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
