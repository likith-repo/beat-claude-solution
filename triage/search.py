"""Web verification via SerpApi (Google). Run by the pipeline in code, never by the model.

The model receives only the structured facts (counts and booleans). Result titles and
snippets are untrusted third-party text and are not passed to it.
"""
import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from .rules import Flag, normalize_company

SERP_URL = "https://serpapi.com/search"
NO_RESULTS_MARKER = "hasn't returned any results"


class SearchBudgetExceeded(RuntimeError):
    pass


@dataclass
class Verification:
    status: str  # ok | skipped | unavailable
    query: str = ""
    results_count: int = 0
    website_domain_in_results: "bool | None" = None
    company_in_titles: "bool | None" = None
    knowledge_graph: bool = False
    error: str = ""
    from_cache: bool = False
    evidence_file: str = ""

    def facts(self):
        """The only thing the model sees about the search."""
        d = asdict(self)
        for k in ("query", "error", "evidence_file", "from_cache"):
            d.pop(k)
        return d


def _host(url):
    if not url:
        return ""
    h = urlparse(url if "://" in url else "//" + url).hostname or ""
    return h.lower().removeprefix("www.")


def build_query(a):
    """Quoted company name plus the website domain; None when there is nothing to search."""
    host = _host(a.website)
    company = re.sub(r'["\n\r]', " ", a.company).strip()
    if company and host:
        return f'"{company}" {host}'
    if company:
        return f'"{company}"'
    return host or None


def _http_get(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "lead-triage-agent/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode())
        except Exception:
            payload = {}
        return e.code, payload
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
        return 0, {"error": str(e)}


class SerpApiClient:
    def __init__(self, api_key, cache_dir=".cache/serpapi", max_live_calls=25, timeout=30,
                 transport=None, sleep=time.sleep):
        if not api_key:
            raise ValueError("SERPAPI_API_KEY is not set")
        self._key, self.cache_dir = api_key, Path(cache_dir)
        self.max_live_calls, self.live_calls = max_live_calls, 0
        self.timeout, self._get, self._sleep = timeout, transport or _http_get, sleep

    def _path(self, query):
        return self.cache_dir / (hashlib.sha256(query.encode()).hexdigest()[:16] + ".json")

    def fetch(self, query):
        """Return (raw_dict, from_cache, cache_path). Raises RuntimeError on unrecoverable failure."""
        path = self._path(query)
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")), True, path
        if self.live_calls >= self.max_live_calls:
            raise SearchBudgetExceeded(f"live search budget of {self.max_live_calls} reached")
        url = SERP_URL + "?" + urllib.parse.urlencode(
            {"engine": "google", "q": query, "num": 10, "api_key": self._key})
        last = ""
        for attempt in range(1, 4):
            status, payload = self._get(url, self.timeout)
            if status in (0, 429) or status >= 500:
                last = f"http {status}"
                self._sleep(2 ** attempt)
                continue
            if status in (401, 403):
                raise RuntimeError(f"http {status}: key rejected")
            err = str(payload.get("error", "")) if isinstance(payload, dict) else "bad payload"
            if status == 200 and (not err or NO_RESULTS_MARKER in err):
                self.live_calls += 1
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload, ensure_ascii=False).replace(self._key, "<redacted>"),
                                encoding="utf-8")
                return payload, False, path
            raise RuntimeError(f"http {status}: {err[:120]}")
        raise RuntimeError(f"search failed after retries ({last})")


def summarize(raw, company, website):
    """Reduce a SerpApi response to facts. Returns a partial Verification (status ok)."""
    organic = raw.get("organic_results") or []
    host = _host(website)
    hosts = [_host(r.get("link", "")) for r in organic]
    comp = normalize_company(company)
    titles = [re.sub(r"[^a-z0-9]", "", (r.get("title") or "").lower()) for r in organic]
    return Verification(
        status="ok", results_count=len(organic),
        website_domain_in_results=(any(h == host or h.endswith("." + host) for h in hosts) if host else None),
        company_in_titles=(any(comp in t for t in titles) if comp else None),
        knowledge_graph=bool(raw.get("knowledge_graph")),
    )


class Verifier:
    def __init__(self, client, evidence_dir=None):
        self.client, self.evidence_dir = client, (Path(evidence_dir) if evidence_dir else None)

    def verify(self, a):
        if a.has("unsafe_link") or a.has("phishing_language"):
            return Verification(status="skipped", error="not searched: security flag on this row")
        query = build_query(a)
        if not query:
            return Verification(status="skipped", error="no company or website to search")
        try:
            raw, cached, path = self.client.fetch(query)
        except SearchBudgetExceeded as e:
            return Verification(status="skipped", query=query, error=str(e))
        except Exception as e:  # any failure degrades to an info flag, never a crash
            return Verification(status="unavailable", query=query, error=f"{type(e).__name__}: {e}"[:200])
        v = summarize(raw, a.company, a.website)
        v.query, v.from_cache = query, cached
        if self.evidence_dir:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            dest = self.evidence_dir / path.name
            dest.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
            v.evidence_file = f"{self.evidence_dir.name}/{path.name}"
        return v


def verification_flags(v, a):
    """Info-level flags only. Search results never reject or qualify a lead on their own."""
    if v.status == "skipped":
        return [Flag("search_skipped", "info", v.error)]
    if v.status == "unavailable":
        return [Flag("search_unavailable", "info", v.error)]
    flags = []
    if v.results_count == 0:
        flags.append(Flag("search_no_results", "info", v.query))
    elif v.website_domain_in_results is False:
        flags.append(Flag("search_domain_not_found", "info", "website domain absent from top results"))
    if v.website_domain_in_results or v.knowledge_graph:
        flags.append(Flag("search_confirms_company", "info", ""))
    return flags
