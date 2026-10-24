"""Deterministic first-pass analysis of a lead row.

Pure functions, no I/O, no model. Lead text is treated as data: this module only
inspects it and raises flags. It never decides QUALIFY/NURTURE/REJECT/ESCALATE;
that is the job of the merge step.

Flag severity:
  critical - must never be auto-qualified; a human (or a hard rule) handles it
  warn     - weakens confidence or is an escalation candidate
  info     - context for the classifier and the human reviewer
"""
import html
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlparse

# Assumed thresholds / lists. Not verified against any Wego or Single Grain policy.
ABSURD_BUDGET_USD = 250_000
LOW_BUDGET_USD = 1_000
LARGE_COMPANY_EMPLOYEES = 1_000

FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "proton.me", "protonmail.com", "aol.com", "gmx.com", "live.com",
}
DISPOSABLE_EMAIL_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "10minutemail.com", "tempmail.com",
    "yopmail.com", "trashmail.com", "sharklasers.com", "getnada.com",
}
EXECUTABLE_EXTENSIONS = {
    ".exe", ".scr", ".bat", ".cmd", ".msi", ".js", ".jar", ".vbs", ".ps1",
    ".dmg", ".apk", ".iso", ".lnk", ".docm", ".xlsm",
}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.I)
TAG_RE = re.compile(r"<[^>]+>")

INJECTION_PATTERNS = [
    re.compile(r"\b(ignore|disregard|forget|override)\b.{0,40}\b(previous|prior|above|earlier|all|any)\b"
               r".{0,40}\b(instructions?|prompts?|rules|guidelines)\b", re.I),
    re.compile(r"\b(classify|mark|label|score|rate|treat|set|output|return)\b.{0,40}"
               r"\b(this|the)\s+(lead|submission|row|record|entry)\b.{0,30}\b(as|to)\b", re.I),
    re.compile(r"\bconfidence\s*(of|=|:|to|at)?\s*(1(\.0+)?|100\s*%)", re.I),
    re.compile(r"\b(system\s+prompt|developer\s+message|you\s+are\s+(now\s+)?(an?\s+)?(ai|assistant|model|bot))\b", re.I),
    re.compile(r"\b(decision|verdict|output)\s*[:=]\s*(qualify|escalate|reject|nurture)\b", re.I),
]
SENSITIVE_PATTERNS = [
    re.compile(r"\bgdpr\b|\bccpa\b|\bpdpa\b|\bpdpl\b", re.I),
    re.compile(r"\barticle\s*17\b|right\s+to\s+(be\s+forgotten|erasure)", re.I),
    re.compile(r"\b(delete|erase|remove)\b.{0,30}\b(personal\s+)?(data|information)\b.{0,40}\b(about|of|on)\s+me\b", re.I),
    re.compile(r"\bdata\s+subject\b|\bsubject\s+access\s+request\b", re.I),
    re.compile(r"\b(lawsuit|attorney|legal\s+action|subpoena|cease\s+and\s+desist)\b", re.I),
]
PHISHING_PATTERNS = [
    re.compile(r"overdue\s+invoice|avoid\s+service\s+interruption|download\s+the\s+attached"
               r"|verify\s+your\s+account|wire\s+transfer|gift\s+card|update\s+your\s+payment", re.I),
]
ASSET_REQUEST_RE = re.compile(
    r"\b(send|share|provide|email)\b.{0,40}\b(deck|methodology|case\s+stud\w*|past\s+client|"
    r"client\s+results|pricing\s+sheet|rate\s+card|references)\b", re.I)
CLAIM_RE = re.compile(
    r"\bfortune\s*\d+\b|\b(vp|svp|ceo|cfo|cmo|coo|cto|director|head)\b(\s+of\b)?", re.I)
URGENCY_RE = re.compile(r"\b(ready to sign|this week|asap|urgent(ly)?|immediately|today only)\b", re.I)
EMPLOYEES_RE = re.compile(r"(\d[\d,]*)[\s-]*(?:employees?|staff)\b", re.I)
PLACEHOLDER_TOKEN_RE = re.compile(r"\b(asdf+\w*|qwert\w*|test(ing)?|lorem|dummy|fake|foo|xxx+)\b", re.I)
ES_STOPWORDS = {"somos", "buscamos", "una", "para", "que", "con", "tenemos", "presupuesto",
                "agencia", "de", "en", "y", "los", "las", "nuestro", "estamos"}


@dataclass
class Flag:
    code: str
    severity: str
    detail: str = ""


@dataclass
class Analysis:
    lead_id: str
    name: str
    email: str
    company: str
    website: str
    source: str
    budget_usd: "float | None"
    budget_status: str
    timestamp_valid: bool
    message_original: str
    message_clean: str
    language: str
    urls: list = field(default_factory=list)
    flags: list = field(default_factory=list)
    duplicate_of: "str | None" = None
    verification: "dict | None" = None

    def has(self, code):
        return any(f.code == code for f in self.flags)

    def codes(self):
        return [f.code for f in self.flags]

    def max_severity(self):
        order = {"info": 0, "warn": 1, "critical": 2}
        return max((f.severity for f in self.flags), key=order.get, default="none")


def clean_text(text):
    """Strip tags, unescape entities, drop emoji/symbol noise, collapse whitespace."""
    text = html.unescape(TAG_RE.sub(" ", text or ""))
    kept = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat in ("So", "Sk", "Cs", "Cn") or ch in "️‍":
            continue
        if cat in ("Cf", "Cc") and ch not in "\n\t":
            continue
        kept.append(ch)
    return re.sub(r"\s+", " ", "".join(kept)).strip()


def parse_budget(raw):
    """Return (value, status). status: ok | missing | unparseable."""
    s = (raw or "").strip()
    if not s:
        return None, "missing"
    t = s.lower().replace("usd", "").replace("$", "").replace(",", "").replace(" ", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([km]?)", t)
    if not m:
        return None, "unparseable"
    value = float(m.group(1)) * {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2)]
    return value, "ok"


def timestamp_is_valid(raw):
    try:
        datetime.fromisoformat((raw or "").strip().replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _domain_of_email(email):
    return email.rsplit("@", 1)[1].lower() if "@" in email else ""


def _registrable(host):
    parts = [p for p in (host or "").lower().split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else (host or "").lower()


def extract_urls(text):
    return [u.rstrip(".,;:") for u in URL_RE.findall(text or "")]


def defang(url):
    return re.sub(r"^http", "hxxp", url, flags=re.I).replace(".", "[.]")


def detect_language(text):
    words = re.findall(r"[a-záéíóúñü]+", (text or "").lower())
    if len(words) >= 5 and sum(w in ES_STOPWORDS for w in words) >= 3:
        return "es"
    return "en"


def normalize_company(name):
    n = re.sub(r"[^a-z0-9 ]", "", (name or "").lower())
    n = re.sub(r"\b(inc|llc|ltd|corp|co|gmbh|group|company)\b", "", n)
    return re.sub(r"\s+", "", n)


def _is_keyboard_mash(text):
    t = re.sub(r"\s+", "", (text or "").lower())
    return len(t) >= 4 and re.fullmatch(r"(.{2,6})\1+", t) is not None


def analyze_lead(row):
    """Analyze one raw CSV row. Never raises on bad data; bad data becomes flags."""
    flags = []

    def flag(code, severity, detail=""):
        flags.append(Flag(code, severity, detail))

    email = (row.get("email") or "").strip().lower()
    company = (row.get("company") or "").strip()
    website = (row.get("website") or "").strip()
    name = (row.get("name") or "").strip()
    raw_message = row.get("message") or ""
    message = clean_text(raw_message)

    if TAG_RE.search(raw_message) or re.search(r"&\w+;|&#\d+;", raw_message):
        flag("had_html", "info", "markup or entities stripped for analysis")

    # Contact and identity
    domain = _domain_of_email(email)
    if not email:
        flag("missing_email", "warn", "no way to contact the lead")
    elif not EMAIL_RE.match(email):
        flag("invalid_email", "warn", "email does not parse")
    else:
        if domain in DISPOSABLE_EMAIL_DOMAINS:
            flag("disposable_email", "warn", domain)
        elif domain in FREE_EMAIL_DOMAINS:
            flag("free_email", "info", domain)
        if re.match(r"^example\.", domain) or PLACEHOLDER_TOKEN_RE.search(domain.split(".")[0]):
            flag("placeholder_domain", "warn", domain)
    if not company:
        flag("no_company", "info")
    if not website:
        flag("no_website", "info")
    else:
        host = urlparse(website).hostname or ""
        if website.lower().startswith("http://"):
            flag("insecure_website", "info", website)
        if domain and domain not in FREE_EMAIL_DOMAINS | DISPOSABLE_EMAIL_DOMAINS \
                and _registrable(host) != _registrable(domain):
            flag("domain_mismatch", "warn", f"email {domain} vs website {host}")

    # Budget
    budget, budget_status = parse_budget(row.get("monthly_budget_usd"))
    if budget_status == "missing":
        flag("budget_missing", "warn")
    elif budget_status == "unparseable":
        flag("budget_unparseable", "warn", (row.get("monthly_budget_usd") or "").strip())
    else:
        if budget >= ABSURD_BUDGET_USD:
            flag("budget_absurd", "warn", f"{budget:,.0f}")
        elif budget == 0:
            flag("budget_zero", "info")
        elif budget < LOW_BUDGET_USD:
            flag("budget_low", "info", f"{budget:,.0f}")

    # Timestamp
    ts_ok = timestamp_is_valid(row.get("submitted_at"))
    if not ts_ok:
        flag("invalid_timestamp", "warn", (row.get("submitted_at") or "").strip())

    # Message content
    if not message:
        flag("empty_message", "warn")
    if any(p.search(message) for p in INJECTION_PATTERNS):
        flag("injection_attempt", "critical", "message addresses the system/classifier")
    if any(p.search(message) for p in SENSITIVE_PATTERNS):
        flag("sensitive_privacy_request", "critical", "privacy/legal request; needs a human")
    if any(p.search(message) for p in PHISHING_PATTERNS):
        flag("phishing_language", "critical", "payment/urgency pretext")
    urls = extract_urls(raw_message)
    for u in urls:
        path = urlparse(u).path.lower()
        if any(path.endswith(ext) for ext in EXECUTABLE_EXTENSIONS):
            flag("unsafe_link", "critical", defang(u))
    if ASSET_REQUEST_RE.search(message):
        flag("asset_request", "warn", "asks for decks/results before identifying themselves")
    if URGENCY_RE.search(message):
        flag("urgency_pressure", "info")
    if CLAIM_RE.search(message) and (not company or domain in FREE_EMAIL_DOMAINS | DISPOSABLE_EMAIL_DOMAINS):
        flag("unverifiable_claim", "warn", "seniority or enterprise claim with no verifiable company")
    m = EMPLOYEES_RE.search(message)
    if m and budget is not None:
        employees = int(m.group(1).replace(",", "") or 0)
        if employees >= LARGE_COMPANY_EMPLOYEES and budget < LOW_BUDGET_USD:
            flag("conflicting_signals", "warn",
                 f"{employees:,} employees but monthly budget {budget:,.0f} USD")
    junk_sources = [name, company, domain.split(".")[0], email.split("@")[0]]
    if _is_keyboard_mash(message) or sum(bool(PLACEHOLDER_TOKEN_RE.search(s)) for s in junk_sources) >= 2:
        flag("junk_submission", "warn", "placeholder or keyboard-mash content")

    language = detect_language(message)
    if language != "en":
        flag("non_english", "info", language)

    return Analysis(
        lead_id=(row.get("lead_id") or "").strip(), name=name, email=email, company=company,
        website=website, source=(row.get("source") or "").strip(),
        budget_usd=budget, budget_status=budget_status, timestamp_valid=ts_ok,
        message_original=raw_message, message_clean=message, language=language,
        urls=urls, flags=flags,
    )


def analyze_leads(rows):
    """Analyze all rows and link duplicates by email. Returns one Analysis per row, in order."""
    results = [analyze_lead(r) for r in rows]
    first_by_email = {}
    for a in results:
        if not a.email:
            continue
        first = first_by_email.get(a.email)
        if first is None:
            first_by_email[a.email] = a
            continue
        a.duplicate_of = first.lead_id
        a.flags.append(Flag("duplicate_of", "info", first.lead_id))
        changed = []
        if first.budget_usd != a.budget_usd:
            changed.append("budget")
        c1, c2 = normalize_company(first.company), normalize_company(a.company)
        if c1 and c2 and c1 != c2:
            changed.append("company")
        if changed:
            a.flags.append(Flag("same_contact_changed_details", "warn",
                                f"{first.lead_id} vs {a.lead_id}: {', '.join(changed)} differ"))
    return results
