"""APP-3: deterministic three-valued applicability engine. The LLM never touches this."""
import re
from dataclasses import dataclass, field

from .schema import Condition, Rule

T, F, U = "TRUE", "FALSE", "UNKNOWN"
LABEL = {T: "APPLIES", F: "NOT APPLICABLE", U: "CANNOT DETERMINE"}


def norm_model(s):
    s = str(s).upper().replace(" ", "")
    s = re.sub(r"^AIRBUS", "", s)
    return s


def family(model):
    m = norm_model(model)
    return re.split(r"-", m)[0]


def and3(xs):
    if F in xs:
        return F
    if U in xs:
        return U
    return T


def or3(xs):
    if T in xs:
        return T
    if U in xs:
        return U
    return F


def not3(x):
    return {T: F, F: T, U: U}[x]


@dataclass
class Trace:
    condition: Condition
    result: str
    reason: str


@dataclass
class Verdict:
    label: str
    value: str
    traces: list = field(default_factory=list)
    missing: list = field(default_factory=list)


def _msn(ac):
    try:
        return int(re.sub(r"\D", "", str(ac.get("msn", ""))))
    except ValueError:
        return None


def eval_condition(c: Condition, ac: dict):
    f = c.field
    if f == "external_document":
        doc = ", ".join(map(str, c.values))
        return U, f"applicability defined in {doc}, which is not in the dataset", f"Check {doc}: does it list {ac.get('registration')} (MSN {ac.get('msn')})?"
    if f in ("model", "model_family"):
        m = ac.get("model")
        if not m:
            return U, "aircraft model missing", "Aircraft model is missing in fleet data."
        if f == "model":
            hit = norm_model(m) in {norm_model(v) for v in c.values}
        else:
            hit = family(m) in {norm_model(v) for v in c.values}
        r = T if hit else F
        if c.op == "not_in":
            r = not3(r)
        return r, f"model {m} {'in' if hit else 'not in'} {c.values}", None
    if f == "msn":
        n = _msn(ac)
        if n is None:
            return U, "MSN missing", f"MSN of {ac.get('registration')} is missing in fleet data."
        if c.op == "range":
            hit = (c.min is None or n >= c.min) and (c.max is None or n <= c.max)
            desc = f"MSN {n} {'inside' if hit else 'outside'} {c.min}..{c.max}"
        else:
            vals = {int(re.sub(r'\D', '', str(v))) for v in c.values if re.sub(r'\D', '', str(v))}
            hit = n in vals
            desc = f"MSN {n} {'in' if hit else 'not in'} listed MSNs"
        r = T if hit else F
        if c.op in ("not_in",):
            r = not3(r)
        return r, desc, None
    if f == "engine_model":
        e = ac.get("engine_model")
        if not e:
            return U, "engine model missing", f"Engine model of {ac.get('registration')} is missing in fleet data."
        hit = any(norm_model(v) in norm_model(e) for v in c.values)
        r = T if hit else F
        return (not3(r) if c.op == "not_in" else r), f"engine {e} {'matches' if hit else 'does not match'} {c.values}", None
    if f in ("mods_installed", "parts_installed"):
        have = ac.get(f)
        what = "modification" if f == "mods_installed" else "part number"
        if have is None:
            vals = ", ".join(map(str, c.values))
            return U, f"{f} unknown", f"Is {what} {vals} installed on {ac.get('registration')} (MSN {ac.get('msn')})? Not in fleet data."
        have_n = {norm_model(x) for x in have}
        hit = any(norm_model(v) in have_n for v in c.values)
        r = T if hit else F
        if c.op == "not_contains":
            r = not3(r)
        return r, f"{what} {c.values} {'present' if hit else 'absent'} in records", None
    return U, f"unsupported field {f}", None


def evaluate(rule: Rule, ac: dict) -> Verdict:
    traces, missing = [], []

    def run(c):
        r, why, q = eval_condition(c, ac)
        traces.append(Trace(c, r, why))
        if r == U and q:
            missing.append(q)
        return r

    group_vals = [and3([run(c) for c in g.conditions]) for g in rule.groups] or [U]
    v = or3(group_vals)
    if rule.exclusions and v != F:
        excl = or3([run(c) for c in rule.exclusions])
        v = and3([v, not3(excl)])
    if rule.unresolved and v != F:
        v = U
        missing += [f"Unresolved applicability text needs manual check: \"{u}\"" for u in rule.unresolved]
    return Verdict(LABEL[v], v, traces, list(dict.fromkeys(missing)))
