"""Load the leads CSV, fingerprint it, and check its schema."""
import csv
import hashlib
from pathlib import Path

REQUIRED_COLUMNS = [
    "lead_id", "submitted_at", "name", "email", "company",
    "website", "monthly_budget_usd", "message", "source",
]


class SchemaError(ValueError):
    pass


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_leads(path):
    """Return (rows, sha256). Every row is a dict of strings; none are dropped."""
    path = Path(path)
    digest = sha256_file(path)
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in REQUIRED_COLUMNS if c not in (reader.fieldnames or [])]
        if missing:
            raise SchemaError(f"missing columns: {', '.join(missing)}")
        rows = [{k: (v if v is not None else "") for k, v in row.items()} for row in reader]
    return rows, digest
