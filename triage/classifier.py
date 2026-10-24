"""Classifiers: OpenRouter (LLM) and Offline (rules only). Both return a ModelResult."""
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .prompt import build_messages

DECISIONS = {"QUALIFY", "NURTURE", "REJECT", "ESCALATE"}
URL_IN_TEXT = re.compile(r"https?://\S+", re.I)


@dataclass
class ModelResult:
    ok: bool
    decision: str = ""
    reason: str = ""
    confidence: float = 0.0
    manipulation_detected: bool = False
    error: str = ""
    raw_output: str = ""
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    attempts: int = 0
    prompt_messages: list = field(default_factory=list)
    request_params: dict = field(default_factory=dict)  # request body minus messages; shows tools were not sent


def load_env(path=".env"):
    """Tiny .env loader (no dependency). Does not override existing environment."""
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def parse_model_json(text):
    """Extract and validate the model's JSON answer. Returns (dict|None, error)."""
    if not text:
        return None, "empty content"
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        return None, "no JSON object found"
    try:
        obj = json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e}"
    decision = str(obj.get("decision", "")).strip().upper()
    if decision not in DECISIONS:
        return None, f"bad decision: {obj.get('decision')!r}"
    try:
        conf = float(obj.get("confidence"))
    except (TypeError, ValueError):
        return None, "confidence not a number"
    if not 0 <= conf <= 1:
        return None, f"confidence out of range: {conf}"
    reason = URL_IN_TEXT.sub("[link]", str(obj.get("reason", "")).strip())[:300]
    if not reason:
        return None, "empty reason"
    return {"decision": decision, "reason": reason, "confidence": conf,
            "manipulation_detected": bool(obj.get("manipulation_detected", False))}, ""


def _http_post(url, headers, body, timeout):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode())
        except Exception:
            payload = {}
        return e.code, payload
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"error": {"message": str(e)}}


class OpenRouterClassifier:
    offers_tools = False  # the model is never given tools
    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, api_key, model, max_tokens=3000, timeout=120, transport=None, sleep=time.sleep):
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY is not set")
        self.model, self.max_tokens, self.timeout = model, max_tokens, timeout
        self._key, self._post, self._sleep = api_key, transport or _http_post, sleep

    def classify(self, analysis):
        messages = build_messages(analysis)
        res = ModelResult(ok=False, prompt_messages=messages)
        body = {"model": self.model, "messages": messages, "temperature": 0, "max_tokens": self.max_tokens}
        res.request_params = {k: v for k, v in body.items() if k != "messages"}
        headers = {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json",
                   "X-Title": "lead-triage-agent"}
        started = time.time()
        for attempt in range(1, 5):
            res.attempts = attempt
            status, payload = self._post(self.URL, headers, body, self.timeout)
            if status in (0, 429) or status >= 500:
                res.error = f"http {status}: {(payload.get('error') or {}).get('message', '')}"[:200]
                self._sleep(2 ** attempt)
                continue
            if status != 200:
                res.error = f"http {status}: {(payload.get('error') or {}).get('message', '')}"[:200]
                break
            try:
                content = payload["choices"][0]["message"].get("content")
            except (KeyError, IndexError, TypeError):
                content = None
            res.raw_output = content or ""
            res.usage = payload.get("usage", {}) or {}
            parsed, err = parse_model_json(content)
            if parsed:
                res.ok, res.error = True, ""
                res.decision, res.reason = parsed["decision"], parsed["reason"]
                res.confidence = parsed["confidence"]
                res.manipulation_detected = parsed["manipulation_detected"]
                break
            res.error = err
            if attempt >= 2:  # one re-ask for malformed output, then give up
                break
        res.latency_ms = int((time.time() - started) * 1000)
        return res


class OfflineClassifier:
    """Rules-only stand-in so the pipeline runs with no key and no network."""
    offers_tools = False

    def classify(self, a):
        b = a.budget_usd or 0
        if b >= 5000:
            d, c, r = "QUALIFY", 0.6, "Budget meets the qualify threshold; no model judgment applied (offline mode)."
        elif b >= 1000:
            d, c, r = "NURTURE", 0.55, "Budget is below the qualify threshold; nurture (offline mode)."
        elif a.budget_status == "unparseable":
            d, c, r = "NURTURE", 0.4, "Budget is not numeric; needs discovery (offline mode)."
        else:
            d, c, r = "REJECT", 0.55, "Budget is below the minimum (offline mode)."
        return ModelResult(ok=True, decision=d, reason=r, confidence=c,
                           manipulation_detected=a.has("injection_attempt"), raw_output="(offline)")
