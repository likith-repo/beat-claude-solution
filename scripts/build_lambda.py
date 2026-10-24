"""Build build/lambda.zip: the code only (stdlib + boto3 from the Lambda runtime). Deterministic."""
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INCLUDE = [("triage", "*.py"), ("backend", "*.py"), ("prompts", "*.md"), ("skills", "**/SKILL.md")]
FIXED_TIME = (2026, 1, 1, 0, 0, 0)


def main():
    out = ROOT / "build" / "lambda.zip"
    out.parent.mkdir(exist_ok=True)
    files = sorted(f for d, pat in INCLUDE for f in (ROOT / d).glob(pat) if "__pycache__" not in f.parts)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            info = zipfile.ZipInfo(f.relative_to(ROOT).as_posix(), FIXED_TIME)
            info.external_attr = 0o644 << 16
            z.writestr(info, f.read_bytes())
    print(f"wrote {out} ({out.stat().st_size} bytes, {len(files)} files)")
    for f in files:
        print("  ", f.relative_to(ROOT))


if __name__ == "__main__":
    sys.exit(main())
