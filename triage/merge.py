"""Guardrail merge. Hard rules beat the model; the model decides only what is left.

Reason strings for rule outcomes are templates: they never echo lead text.
"""
from dataclasses import dataclass, field

MAX_CONF = 0.95
LOW_BUDGET_USD = 1_000
LOW_CONF_ESCALATE = 0.5
QUALIFY_BLOCKERS = {
    "budget_unparseable", "budget_missing", "empty_message", "asset_request",
    "disposable_email", "same_contact_changed_details", "domain_mismatch", "placeholder_domain",
    "invalid_timestamp",
}
DATA_QUALITY_ONLY = {"empty_message", "invalid_timestamp"}  # never a reason to escalate on their own


@dataclass
class Decision:
    lead_id: str
    decision: str
    reason: str
    confidence: float
    decided_by: str
    route_to: str = ""
    flags: list = field(default_factory=list)
    duplicate_of: "str | None" = None
    model_decision: str = ""
    model_confidence: float = 0.0
    manipulation_detected: bool = False


def _mk(a, m, decision, reason, conf, by, route=""):
    return Decision(
        lead_id=a.lead_id, decision=decision, reason=reason, confidence=round(min(conf, MAX_CONF), 2),
        decided_by=by, route_to=route, flags=a.codes(), duplicate_of=a.duplicate_of,
        model_decision=(m.decision if m and m.ok else ""), model_confidence=(m.confidence if m and m.ok else 0.0),
        manipulation_detected=a.has("injection_attempt") or bool(m and m.ok and m.manipulation_detected),
    )


def _identity_fields_ok(a):
    """Company and website given, email on the company domain. Web search is not required."""
    return bool(a.company and a.website
                and not any(a.has(c) for c in ("free_email", "disposable_email", "domain_mismatch", "placeholder_domain")))


def _consistent_identity(a):
    return _identity_fields_ok(a) and a.budget_status == "ok" and (a.budget_usd or 0) >= LOW_BUDGET_USD


def _other_concerns(a, ignore=()):
    skip = DATA_QUALITY_ONLY | set(ignore)
    return any(f.severity in ("warn", "critical") and f.code not in skip for f in a.flags)


def merge(a, m):
    """a: Analysis, m: ModelResult or None. Returns a Decision. Never raises on bad input."""
    # 1. Critical safety rules.
    if a.has("unsafe_link") or a.has("phishing_language"):
        return _mk(a, m, "REJECT",
                   "Not a lead: phishing or malware pretext (payment demand with an executable link); report to InfoSec and do not open any link.",
                   0.95, "rule:security", "InfoSec (report only)")
    if a.has("sensitive_privacy_request"):
        return _mk(a, m, "REJECT",
                   "Not a lead: privacy or legal request (for example data erasure); routed to Privacy/DPO for action within the legal deadline, with no automated reply or deletion.",
                   0.95, "rule:privacy", "Privacy/DPO (action required)")
    if a.has("injection_attempt"):
        low_value = a.has("budget_low") or a.has("budget_zero") or (m and m.ok and m.decision == "REJECT")
        if low_value:
            return _mk(a, m, "REJECT",
                       "Message tries to dictate the outcome (treated as data and ignored); on its merits the budget is below the minimum, flagged for review.",
                       0.85, "rule:injection", "Sales ops (manipulation flag)")
        return _mk(a, m, "ESCALATE",
                   "Message tries to dictate the outcome (treated as data and ignored); a human should assess the real lead.",
                   0.8, "rule:injection", "Sales ops (manipulation flag)")

    # 2. Junk is rejected outright.
    if a.has("junk_submission"):
        return _mk(a, m, "REJECT", "Test lead, not real: placeholder or keyboard-mash content with no usable contact or need.",
                   0.9, "rule:junk")

    # 3. Missing, malformed or conflicting inputs go to a human.
    for code, reason in (
        ("missing_email", "No email address, so the lead cannot be contacted; a human should recover contact details."),
        ("invalid_email", "Email address is malformed; a human should verify the contact."),
        ("unverifiable_claim", "Seniority or enterprise claim cannot be verified from the submitted details; a human should check."),
    ):
        if a.has(code):
            return _mk(a, m, "ESCALATE", reason, 0.8, f"rule:{code}", "Sales ops")

    if a.has("conflicting_signals"):
        if _identity_fields_ok(a) and not _other_concerns(a, ignore={"conflicting_signals"}):
            return _mk(a, m, "NURTURE",
                       "Company size and stated budget conflict (budget may be a typo) but identity details are consistent; "
                       "nurture and confirm the budget in discovery. The size claim is not verified.",
                       0.6, "rule:conflicting_signals_nurture")
        return _mk(a, m, "ESCALATE", "Company size and stated budget conflict alongside other concerns; a human should confirm.",
                   0.8, "rule:conflicting_signals", "Sales ops")

    # 4. Model unavailable or unsure.
    if m is None or not m.ok:
        err = (m.error if m else "no classifier result")[:80]
        return _mk(a, m, "ESCALATE", f"Classifier unavailable or invalid output ({err}); needs a human.",
                   0.5, "rule:classifier_failure", "Sales ops")
    if m.confidence < LOW_CONF_ESCALATE and m.decision != "ESCALATE":
        return _mk(a, m, "ESCALATE", f"Classifier was not confident ({m.confidence:.2f}); needs a human.",
                   m.confidence, "rule:low_confidence", "Sales ops")

    # 5. Duplicates: same contact as an earlier lead.
    if a.duplicate_of and not a.has("same_contact_changed_details"):
        return _mk(a, m, "NURTURE",
                   f"Duplicate of {a.duplicate_of} (same contact); merge into the original record and note any new interest.",
                   0.8, "rule:duplicate")

    # 6. Model decision, with qualify blockers.
    decision, conf, by, reason = m.decision, min(m.confidence, MAX_CONF), "model", m.reason
    if a.has("same_contact_changed_details"):
        reason = f"{reason} Details conflict with earlier submission {a.duplicate_of}; verify."[:300]
    if decision == "ESCALATE" and _consistent_identity(a) and not _other_concerns(a):
        # The model was unsure but no concrete risk exists: a human can qualify it from the nurture queue.
        decision, conf, by = "NURTURE", min(conf, 0.65), "model+rule:soft_escalate"
        reason = ("No message to assess, but company, website, email domain and budget are consistent; "
                  "nurture and ask discovery questions." if a.has("empty_message") else
                  "No concrete risk flags and identity details are consistent; intent is unclear "
                  "(for example an agency researching for a client), so nurture and let a human qualify it.")
    if decision == "QUALIFY":
        blockers = sorted(QUALIFY_BLOCKERS & set(a.codes()))
        if blockers:
            decision, conf, by = "NURTURE", min(conf, 0.7), "model+cap"
            reason = f"{reason} Held at nurture pending: {', '.join(blockers)}."[:300]
    if a.has("urgency_pressure") and decision == "QUALIFY":
        conf = min(conf, 0.75)
    if decision == "QUALIFY" and (a.has("search_no_results") or a.has("search_domain_not_found")):
        conf = min(conf, 0.7)
        reason = f"{reason} Company not confirmed by web search."[:300]
    route = "Sales ops" if decision == "ESCALATE" else ""
    return _mk(a, m, decision, reason, conf, by, route)
