"""Run the triage agent on a leads CSV.

  python3 scripts/run.py --offline                 # rules only, no key, no network
  python3 scripts/run.py --out out/run1            # OpenRouter model (reads .env)
"""
import argparse
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.classifier import OfflineClassifier, OpenRouterClassifier, load_env  # noqa: E402
from triage.pipeline import run  # noqa: E402
from triage.search import SerpApiClient, Verifier  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="leads CSV to process")
    ap.add_argument("--out", default="out/latest")
    ap.add_argument("--offline", action="store_true", help="rules-only classifier; no API key needed")
    ap.add_argument("--no-search", action="store_true", help="skip SerpApi web verification")
    ap.add_argument("--max-searches", type=int, default=25, help="cap on live (uncached) searches per run")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between model calls (rate limits)")
    args = ap.parse_args()

    load_env()
    if args.offline:
        clf, mode, model = OfflineClassifier(), "offline", ""
    else:
        model = os.environ.get("OPENROUTER_MODEL", "nvidia/nemotron-3-super-120b-a12b")
        clf = OpenRouterClassifier(os.environ.get("OPENROUTER_API_KEY", ""), model)
        mode = "online"

    verifier = None
    if not args.offline and not args.no_search:
        key = os.environ.get("SERPAPI_API_KEY", "")
        if key:
            verifier = Verifier(SerpApiClient(key, max_live_calls=args.max_searches),
                                evidence_dir=Path(args.out) / "search_evidence")
        else:
            print("SERPAPI_API_KEY not set: running without web verification", file=sys.stderr)

    decisions, digest, run_id = run(args.input, args.out, clf, mode, model,
                                    delay=0 if args.offline else args.delay, verifier=verifier)
    print(f"\nrun_id: {run_id}\nsha256: {digest}\nwrote:  {args.out}/decisions.csv, decisions.json, run_log.jsonl")
    print("counts:", dict(Counter(d.decision for d in decisions)))


if __name__ == "__main__":
    main()
