"""Run the full pipeline over a fixture and write decisions + run log."""
import csv
import json
import subprocess
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from .ingest import read_leads
from .merge import Decision, merge
from .prompt import PROMPT_FILE, SKILL_FILE, prompt_fingerprint
from .rules import analyze_leads, defang
from .search import Verifier, verification_flags


def _git_sha():
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True,
                              timeout=5).stdout.strip() or None
    except Exception:
        return None


def run(input_path, out_dir, classifier, mode, model_name="", delay=0.0, log=print, verifier=None, on_row=None):
    rows, digest = read_leads(input_path)
    results = analyze_leads(rows)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + digest[:8]
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    decisions, log_lines = [], []
    log_lines.append({"type": "header", "run_id": run_id, "fixture": str(input_path), "fixture_sha256": digest,
                      "rows": len(rows), "mode": mode, "model": model_name, "git_sha": _git_sha(),
                      "prompt_sha256": prompt_fingerprint(), "web_search": ("serpapi:google" if verifier else None),
                      "model_tool_access": ("enabled" if getattr(classifier, "offers_tools", False) else "none"),
                      "tools_run_by_pipeline": (["serpapi.google_search"] if verifier else []),
                      "prompt_files": [str(Path(PROMPT_FILE).relative_to(PROMPT_FILE.parent.parent)),
                                       str(Path(SKILL_FILE).relative_to(SKILL_FILE.parent.parent.parent))],
                      "brief_version": "2026-07", "started_at": datetime.now(timezone.utc).isoformat()})
    for i, a in enumerate(results):
        t0 = time.time()
        tool_calls, search_ms = [], 0
        if verifier:
            ts0 = time.time()
            v = verifier.verify(a)
            search_ms = int((time.time() - ts0) * 1000)
            a.verification = {"facts": v.facts(), "query": v.query, "status": v.status, "error": v.error,
                              "from_cache": v.from_cache, "evidence_file": v.evidence_file}
            a.flags.extend(verification_flags(v, a))
            tool_calls.append({
                "tool": "serpapi.google_search", "invoked_by": "pipeline", "query": v.query,
                "status": v.status, "from_cache": v.from_cache, "latency_ms": search_ms,
                "result_count": (v.results_count if v.status == "ok" else None),
                "evidence_file": v.evidence_file, "error": v.error,
            })
        try:
            m = classifier.classify(a)
        except Exception as e:  # a failed row becomes ESCALATE, never a dropped row
            m = None
            err = f"{type(e).__name__}: {e}"[:200]
        d = merge(a, m)
        decisions.append(d)
        log_lines.append({
            "type": "row", "run_id": run_id, "lead_id": a.lead_id,
            "rule_flags": [asdict(f) for f in a.flags], "duplicate_of": a.duplicate_of,
            "budget_usd": a.budget_usd, "budget_status": a.budget_status,
            "tool_calls": tool_calls,
            "web_verification": a.verification,
            "unsafe_urls_defanged": [defang(u) for u in a.urls if any(f.code == "unsafe_link" for f in a.flags)],
            "prompt_messages": (m.prompt_messages if m else None),
            "raw_model_output": (m.raw_output if m else None),
            "model_ok": (m.ok if m else False), "model_error": (m.error if m else err),
            "model_parsed": ({"decision": m.decision, "reason": m.reason, "confidence": m.confidence,
                              "manipulation_detected": m.manipulation_detected} if m and m.ok else None),
            "model_request_params": (m.request_params if m else None),
            "model_tools_offered": bool(m and "tools" in (m.request_params or {})),
            "usage": (m.usage if m else None), "attempts": (m.attempts if m else 0),
            "search_latency_ms": search_ms, "model_latency_ms": (m.latency_ms if m else 0),
            "latency_ms": int((time.time() - t0) * 1000),
            "final": {"decision": d.decision, "confidence": d.confidence, "decided_by": d.decided_by,
                      "route_to": d.route_to, "reason": d.reason},
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        if on_row:
            on_row(i + 1, len(results), d)
        log(f"[{i + 1}/{len(results)}] {a.lead_id} -> {d.decision} ({d.confidence}) by {d.decided_by}")
        if delay:
            time.sleep(delay)

    if len(decisions) != len(rows):
        raise RuntimeError(f"row count mismatch: {len(decisions)} decisions for {len(rows)} rows")

    _write_csv(out / "decisions.csv", decisions)
    (out / "decisions.json").write_text(json.dumps(
        {"run_id": run_id, "fixture_sha256": digest, "mode": mode, "model": model_name,
         "decisions": [asdict(d) for d in decisions]}, indent=2, ensure_ascii=False), encoding="utf-8")
    with (out / "run_log.jsonl").open("w", encoding="utf-8") as fh:
        for rec in log_lines:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return decisions, digest, run_id


def _write_csv(path, decisions):
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["lead_id", "decision", "reason", "confidence", "decided_by", "route_to", "flags", "duplicate_of"])
        for d in decisions:
            w.writerow([d.lead_id, d.decision, d.reason, d.confidence, d.decided_by, d.route_to,
                        ";".join(d.flags), d.duplicate_of or ""])
