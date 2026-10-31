from rules.engine import evaluate
from rules.schema import Rule
from rules.validate import validate

SRC = ("This AD applies to Airbus Model A320-211, -212, and -214 airplanes, manufacturer serial numbers 1516 to 1754 inclusive, "
       "except those on which Airbus modification 28155 has been embodied.")


def rule(**kw):
    base = dict(doc_id="t", scope="certain", groups=[{"conditions": [
        {"field": "model", "op": "in", "values": ["A320-211", "A320-212", "A320-214"], "quote": "Model A320-211, -212, and -214"},
        {"field": "msn", "op": "range", "min": 1516, "max": 1754, "quote": "serial numbers 1516 to 1754 inclusive"},
    ]}], exclusions=[{"field": "mods_installed", "op": "contains", "values": ["28155"], "quote": "modification 28155"}])
    base.update(kw)
    return Rule.model_validate(base)


def ac(**kw):
    a = dict(registration="9V-T", model="A320-214", msn=1600, mods_installed=[])
    a.update(kw)
    return a


def test_inside_range():
    assert evaluate(rule(), ac()).label == "APPLIES"


def test_boundaries():
    assert evaluate(rule(), ac(msn=1516)).label == "APPLIES"
    assert evaluate(rule(), ac(msn=1754)).label == "APPLIES"
    assert evaluate(rule(), ac(msn=1515)).label == "NOT APPLICABLE"
    assert evaluate(rule(), ac(msn=1755)).label == "NOT APPLICABLE"


def test_wrong_variant():
    assert evaluate(rule(), ac(model="A320-232")).label == "NOT APPLICABLE"


def test_exclusion():
    assert evaluate(rule(), ac(mods_installed=["28155"])).label == "NOT APPLICABLE"


def test_missing_mod_data_is_unknown():
    v = evaluate(rule(), ac(mods_installed=None))
    assert v.label == "CANNOT DETERMINE"
    assert any("28155" in m for m in v.missing)


def test_missing_msn_but_wrong_model_still_false():
    assert evaluate(rule(), ac(model="A330-243", msn=None)).label == "NOT APPLICABLE"


def test_missing_msn_unknown():
    assert evaluate(rule(), ac(msn=None)).label == "CANNOT DETERMINE"


def test_unresolved_forces_unknown():
    assert evaluate(rule(unresolved=["if the APU is fitted with ..."]), ac()).label == "CANNOT DETERMINE"


def test_external_document_unknown():
    r = rule(groups=[{"conditions": [
        {"field": "model", "op": "in", "values": ["A320-214"], "quote": "A320-214"},
        {"field": "external_document", "op": "requires_external", "values": ["EASA AD 2026-0001"], "quote": "x"}]}], exclusions=[])
    v = evaluate(r, ac())
    assert v.label == "CANNOT DETERMINE" and "EASA AD 2026-0001" in v.missing[0]
    assert evaluate(r, ac(model="A330-243")).label == "NOT APPLICABLE"


def test_validator_accepts_abbreviated_models():
    assert validate(rule(), SRC) == []


def test_validator_rejects_invented_value():
    r = rule()
    r.groups[0].conditions[0].values.append("A320-233")
    assert any("A320-233" in e for e in validate(r, SRC))


def test_validator_rejects_fake_quote():
    r = rule()
    r.groups[0].conditions[1].quote = "serial numbers 1000 to 2000 inclusive"
    assert validate(r, SRC)


def cert_rule(limit):
    return Rule.model_validate(dict(doc_id="t", scope="certain", groups=[{"conditions": [
        {"field": "model", "op": "in", "values": ["A330-941"], "quote": "A330-941"},
        {"field": "cert_date", "op": "on_or_before", "values": [limit], "quote": "issued on or before"}]}]))


def test_cert_date_revision_changes_scope():
    a = ac(model="A330-941", cert_date="2025-03-10")
    assert evaluate(cert_rule("2024-10-01"), a).label == "NOT APPLICABLE"
    assert evaluate(cert_rule("2025-12-19"), a).label == "APPLIES"
    assert evaluate(cert_rule("2025-03-10"), a).label == "APPLIES"


def test_cert_date_missing_unknown():
    assert evaluate(cert_rule("2025-12-19"), ac(model="A330-941", cert_date=None)).label == "CANNOT DETERMINE"


def test_validator_accepts_written_date():
    r = cert_rule("2025-12-19")
    r.groups[0].conditions[1].quote = "issued on or before December 19, 2025"
    r.groups[0].conditions[0].quote = "Model A330-941 airplanes"
    assert validate(r, "Model A330-941 airplanes with a certificate issued on or before December 19, 2025.") == []


# ---- exclusion edge cases (regex extractor) ----
from rules.extract import regex_extract


def _label(text, **ac_kw):
    r = regex_extract("t", text)
    assert validate(r, text) == [], validate(r, text)
    a = dict(registration="9V-T", model="A320-214", msn=1600, mods_installed=None)
    a.update(ac_kw)
    return evaluate(r, a).label


def test_except_model_is_not_read_as_scope():
    t = "(c) Applicability\n\nThis AD applies to all Airbus SAS Model A320 airplanes, certificated in any category, except Model A320-251N airplanes."
    assert _label(t, model="A320-214") == "APPLIES"
    assert _label(t, model="A320-251N") == "NOT APPLICABLE"
    assert _label(t, model="A330-243") == "NOT APPLICABLE"


def test_except_abbreviated_variant():
    t = "(c) Applicability\n\nThis AD applies to all Airbus SAS Model A321-211, -231, and -251N airplanes, certificated in any category, except Model -251N airplanes."
    assert _label(t, model="A321-211") == "APPLIES"
    assert _label(t, model="A321-251N") == "NOT APPLICABLE"


def test_except_msn_list():
    t = "(c) Applicability\n\nThis AD applies to Airbus SAS Model A321-211 airplanes, certificated in any category, all manufacturer serial numbers except MSN 1681 and 1700."
    assert _label(t, model="A321-211", msn=1681) == "NOT APPLICABLE"
    assert _label(t, model="A321-211", msn=1500) == "APPLIES"


def test_except_modification_asks_when_unknown():
    t = ("(c) Applicability\n\nThis AD applies to all Airbus SAS Model A330-243 and -343 airplanes, certificated in any category, "
         "except those on which Airbus modification 201234 has been embodied in production.")
    assert _label(t, model="A330-243", mods_installed=None) == "CANNOT DETERMINE"
    assert _label(t, model="A330-243", mods_installed=["201234"]) == "NOT APPLICABLE"
    assert _label(t, model="A330-243", mods_installed=[]) == "APPLIES"


def test_msn_range_inclusive():
    t = "(c) Applicability\n\nThis AD applies to Airbus SAS Model A320-214 airplanes, certificated in any category, manufacturer serial numbers 1000 through 1500 inclusive."
    assert _label(t, msn=1000) == "APPLIES"
    assert _label(t, msn=1500) == "APPLIES"
    assert _label(t, msn=1501) == "NOT APPLICABLE"


def test_coverage_catches_dropped_model():
    from rules.validate import coverage_gaps
    src = "This AD applies to all Airbus SAS Model A330-243 and A330-343 airplanes, certificated in any category."
    r = Rule.model_validate(dict(doc_id="t", scope="all", groups=[{"conditions": [
        {"field": "model", "op": "in", "values": ["A330-243"], "quote": "Model A330-243"}]}]))
    assert validate(r, src) == []
    assert coverage_gaps(r, src) == ["A330-343"]


def test_coverage_catches_dropped_exception():
    from rules.validate import coverage_gaps
    src = "This AD applies to all Airbus SAS Model A320 airplanes, certificated in any category, except Model A320-251N airplanes."
    r2 = Rule.model_validate(dict(doc_id="t", scope="all", groups=[{"conditions": [
        {"field": "model", "op": "in", "values": ["A320-214"], "quote": "A320"}]}]))
    assert "A320-251N" in coverage_gaps(r2, src)


def test_coverage_catches_dropped_msn():
    from rules.validate import coverage_gaps
    src = "This AD applies to Airbus SAS Model A321-211 airplanes, manufacturer serial numbers 1000 through 1500 inclusive."
    r = Rule.model_validate(dict(doc_id="t", scope="certain", groups=[{"conditions": [
        {"field": "model", "op": "in", "values": ["A321-211"], "quote": "Model A321-211"}]}]))
    assert coverage_gaps(r, src) == ["MSN 1000", "MSN 1500"]
