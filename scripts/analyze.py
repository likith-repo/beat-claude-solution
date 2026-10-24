"""Step 1 runner: deterministic analysis only (no model). Prints flags per lead."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from triage.ingest import read_leads  # noqa: E402
from triage.rules import analyze_leads  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default="fixtures/inbound_leads.csv")
    args = ap.parse_args()

    rows, digest = read_leads(args.input)
    results = analyze_leads(rows)
    print(f"fixture: {args.input}\nsha256:  {digest}\nrows:    {len(rows)}\n")
    for a in results:
        flags = ", ".join(f"{f.code}[{f.severity[0]}]" for f in a.flags) or "-"
        print(f"{a.lead_id:6} {a.max_severity():8} {flags}")
    print("\nseverity: c=critical w=warn i=info")


if __name__ == "__main__":
    main()
