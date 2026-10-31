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
    return re.match(r"^([A-Z]*\d{3})", m).group(1) if re.match(r"^([A-Z]*\d{3})", m) else re.split(r"-", m)[0]


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
    questions: list = field(default_factory=list)


def _msn(ac):
    try:
        return int(re.sub(r"\D", "", str(ac.get("msn", ""))))
    except ValueError:
        return None


def unresolved_key(text):
    import hashlib
    return "unres:" + hashlib.sha1(" ".join(text.split()).lower().encode()).hexdigest()[:10]


def short_doc(d):
    return re.sub(r"European Union Aviation Safety Agency \(EASA\)", "EASA", d)


def eval_condition(c: Condition, ac: dict):
    """Returns (value, plain-language reason, question-or-None). Engineer answers live in ac['facts']."""
    f = c.field
    reg, msn = ac.get("registration"), ac.get("msn")
    facts = ac.get("facts") or {}
    if f == "external_document":
        doc = ", ".join(map(str, c.values))
        key = f"ext:{doc}"
        if key in facts:
            return (T if facts[key] else F), f"{short_doc(doc)} {'lists' if facts[key] else 'does not list'} this aircraft (engineer answer)", None
        return U, f"the exact list is in {short_doc(doc)}, which is not in the dataset", \
            {"key": key, "kind": "yesno", "text": f"Does {short_doc(doc)} list {reg} (MSN {msn})?"}
    if f in ("model", "model_family"):
        m = ac.get("model")
        if not m:
            return U, "aircraft model unknown", {"key": "model", "kind": "text", "text": f"What is the model of {reg}?"}
        if f == "model":
            hit = norm_model(m) in {norm_model(v) for v in c.values}
        else:
            hit = family(m) in {norm_model(v) for v in c.values}
        r = T if hit else F
        if c.op == "not_in":
            r = not3(r)
        return r, (f"{m} is listed" if hit else f"{m} is not listed"), None
    if f == "msn":
        n = _msn(ac)
        if n is None:
            return U, "MSN unknown", {"key": "msn", "kind": "text", "text": f"What is the MSN of {reg}?"}
        if c.op == "range":
            hit = (c.min is None or n >= c.min) and (c.max is None or n <= c.max)
            desc = f"MSN {n} is {'inside' if hit else 'outside'} {c.min}–{c.max}"
        else:
            vals = {int(re.sub(r'\D', '', str(v))) for v in c.values if re.sub(r'\D', '', str(v))}
            hit = n in vals
            desc = f"MSN {n} is {'' if hit else 'not '}in the listed MSNs"
        r = T if hit else F
        if c.op in ("not_in",):
            r = not3(r)
        return r, desc, None
    if f == "cert_date":
        d = ac.get("cert_date")
        if not d:
            return U, "certificate date unknown", {"key": "cert_date", "kind": "date",
                                                   "text": f"When was the original airworthiness certificate of {reg} issued?"}
        lim = str(c.values[0])
        word = "before" if c.op == "on_or_before" else "after"
        hit = str(d) <= lim if c.op == "on_or_before" else str(d) >= lim
        return (T if hit else F), f"certificate {d} is {'' if hit else 'not '}on or {word} the {lim} cutoff", None
    if f == "engine_model":
        e = ac.get("engine_model")
        if not e:
            return U, "engine model unknown", {"key": "engine_model", "kind": "text", "text": f"What engine model is on {reg}?"}
        hit = any(norm_model(v) in norm_model(e) for v in c.values)
        r = T if hit else F
        return (not3(r) if c.op == "not_in" else r), f"engine {e} {'matches' if hit else 'does not match'}", None
    if f in ("mods_installed", "parts_installed"):
        what = "modification" if f == "mods_installed" else "part number"
        prefix = "mod:" if f == "mods_installed" else "part:"
        known = [facts[prefix + str(v)] for v in c.values if prefix + str(v) in facts]
        have = ac.get(f)
        if known:
            hit = any(known)
        elif have is not None:
            have_n = {norm_model(x) for x in have}
            hit = any(norm_model(v) in have_n for v in c.values)
        else:
            vals = ", ".join(map(str, c.values))
            return U, f"{what} {vals} status unknown", {"key": prefix + str(c.values[0]), "kind": "yesno",
                                                       "text": f"Is {what} {vals} installed on {reg} (MSN {msn})?"}
        r = T if hit else F
        if c.op == "not_contains":
            r = not3(r)
        return r, f"{what} {', '.join(map(str, c.values))} is {'installed' if hit else 'not installed'}", None
    return U, f"unsupported field {f}", None


def evaluate(rule: Rule, ac: dict) -> Verdict:
    traces, questions = [], []

    def run(c):
        r, why, q = eval_condition(c, ac)
        traces.append(Trace(c, r, why))
        if r == U and q:
            questions.append(q)
        return r

    group_vals = [and3([run(c) for c in g.conditions]) for g in rule.groups] or [U]
    v = or3(group_vals)
    if rule.exclusions and v != F:
        excl = or3([run(c) for c in rule.exclusions])
        v = and3([v, not3(excl)])
    if rule.unresolved and v != F:
        facts = ac.get("facts") or {}
        answers = []
        for u in rule.unresolved:
            k = unresolved_key(u)
            if k in facts:
                answers.append(T if facts[k] else F)
                traces.append(Trace(None, answers[-1], f"engineer: aircraft {'included' if facts[k] else 'excluded'} by \"{u[:80]}\""))
            else:
                answers.append(U)
                questions.append({"key": k, "kind": "include", "text": f"The system could not read this condition. Is {ac.get('registration')} included by it? \"{u}\""})
        v = and3([v] + answers)
    seen, qs = set(), []
    if v != U:
        questions = []
    for q in questions:
        if q["key"] not in seen:
            seen.add(q["key"])
            qs.append(q)
    return Verdict(LABEL[v], v, traces, [q["text"] for q in qs], qs)
