"""Draft a public comment on an NPRM. The model only rephrases facts we pass in; the engineer edits and decides whether to send."""
from .config import MODEL_ANSWER

SYSTEM = """You draft a short public comment from an aircraft operator to the FAA on a proposed airworthiness directive.
Use ONLY the facts given. Do not invent numbers, dates, part numbers or technical claims.
Where the operator must add its own position or data, write a bracketed placeholder like [ENGINEER: state requested change].
Plain, professional tone. Under 200 words. No greeting fluff."""


def facts_block(card, report, operator="[Operator name]"):
    affected = [f"{a['registration']} ({a['model']}, MSN {a['msn']})" for a in report["aircraft"] if a["label"] == "APPLIES"]
    lines = [f"Docket: {card['docket']}", f"Operator: {operator}",
             f"Proposal would require: {card.get('require') or 'see proposal'}",
             f"Operator aircraft affected: {', '.join(affected) or 'none determined yet'}"]
    if card.get("effort"):
        lines.append(f"FAA estimate: up to {card['effort']:g} work-hours per aircraft")
    if card.get("first_time"):
        lines.append(f"Compliance time stated: {card['first_time']}")
    if card.get("revised"):
        for a, b in card["revised"]["words"]:
            lines.append(f"Change in latest version: '{a}' -> '{b}'")
    lines.append(f"Comment deadline: {card.get('due')}")
    return "\n".join(lines)


def template(card, report):
    affected = [f"{a['registration']} ({a['model']}, MSN {a['msn']})" for a in report["aircraft"] if a["label"] == "APPLIES"]
    hours = f" The FAA estimates up to {card['effort']:g} work-hours per aircraft." if card.get("effort") else ""
    return (f"Re: Docket No. {card['docket']}\n\n"
            f"[Operator name] operates {len(affected)} aircraft covered by this proposal: {', '.join(affected)}.{hours}\n\n"
            "Comment: [ENGINEER: state the requested change, e.g. compliance time, scope, or clarification]\n\n"
            "Justification: [ENGINEER: operational impact, fleet data, alternative method]\n")


def draft(client, card, report):
    if not client:
        return template(card, report), "template (no API key)"
    try:
        msg = client.messages.create(model=MODEL_ANSWER, max_tokens=2000, system=SYSTEM,
                                     messages=[{"role": "user", "content": "FACTS:\n" + facts_block(card, report)}])
        text = "\n".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()
        return text or template(card, report), "drafted by Claude from the facts above"
    except Exception as e:
        return template(card, report), f"template (LLM unavailable: {str(e)[:80]})"
