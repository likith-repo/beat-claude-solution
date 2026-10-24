"""Prompt assembly. The wording lives in files, not in code:

  prompts/system_prompt.md          role, untrusted-data rule, output format; has a {{SKILL}} slot
  skills/lead-triage/SKILL.md       triage policy (decision definitions, thresholds)

Lead text goes in as a JSON value inside a delimited data block.
"""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT_FILE = ROOT / "prompts" / "system_prompt.md"
SKILL_FILE = ROOT / "skills" / "lead-triage" / "SKILL.md"
SLOT = "{{SKILL}}"


def _skill_body(text):
    """Drop the YAML frontmatter; the model only needs the policy body."""
    return re.sub(r"\A---\n.*?\n---\n", "", text, flags=re.S).strip()


def load_system_prompt(prompt_file=PROMPT_FILE, skill_file=SKILL_FILE):
    template = Path(prompt_file).read_text(encoding="utf-8")
    if SLOT not in template:
        raise ValueError(f"{prompt_file} is missing the {SLOT} slot")
    skill = _skill_body(Path(skill_file).read_text(encoding="utf-8"))
    return template.replace(SLOT, skill).strip()


def prompt_fingerprint(prompt_file=PROMPT_FILE, skill_file=SKILL_FILE):
    """Hash of the prompt files, recorded in the run log to tie a run to its exact prompt."""
    h = hashlib.sha256()
    for p in (prompt_file, skill_file):
        h.update(Path(p).read_bytes())
    return h.hexdigest()


def lead_payload(a):
    """What the model sees for one lead. No name, no full email (data minimization)."""
    return {
        "lead_id": a.lead_id,
        "source": a.source,
        "company": a.company or None,
        "website": a.website or None,
        "email_domain": a.email.rsplit("@", 1)[1] if "@" in a.email else None,
        "monthly_budget_usd": a.budget_usd,
        "budget_parse_status": a.budget_status,
        "message": a.message_clean,
        "language": a.language,
        "rule_flags": a.codes(),
        "duplicate_of": a.duplicate_of,
        "web_verification": (a.verification or {}).get("facts"),
    }


def build_messages(a, system_prompt=None):
    block = json.dumps(lead_payload(a), ensure_ascii=False, indent=2)
    user = f"Classify this lead.\n<<<LEAD_DATA\n{block}\nLEAD_DATA>>>"
    return [{"role": "system", "content": system_prompt or load_system_prompt()},
            {"role": "user", "content": user}]
