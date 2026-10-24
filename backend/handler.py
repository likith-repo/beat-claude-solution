"""AWS Lambda entry points for the lead triage agent.

  api_handler     API Gateway (HTTP API, payload v2): POST /runs and GET /runs/{id}
  worker_handler  invoked asynchronously by api_handler; runs the pipeline and writes to S3

A run takes minutes, so the API starts it and the page polls. All state lives in S3 under
runs/{run_id}/ (input.csv, progress.json, decisions.csv|json, run_log.jsonl, search_evidence/).
Secrets are read from SSM Parameter Store at run time; they never reach the client.
"""
import hmac
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from triage.classifier import OpenRouterClassifier
from triage.ingest import SchemaError, read_leads
from triage.pipeline import run
from triage.search import SerpApiClient, Verifier

MAX_BYTES = 100_000
MAX_ROWS = 50
RUN_ID_RE = re.compile(r"^[a-f0-9]{12}$")
DOWNLOADS = ("decisions.csv", "decisions.json", "run_log.jsonl")
PRESIGN_SECONDS = 900

_clients = {}
_param_cache = {}


def _aws():
    if not _clients:
        import boto3  # provided by the Lambda runtime
        _clients.update(s3=boto3.client("s3"), ssm=boto3.client("ssm"), lam=boto3.client("lambda"))
    return _clients


def _param(aws, env_name):
    name = os.environ[env_name]
    if name not in _param_cache:
        _param_cache[name] = aws["ssm"].get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    return _param_cache[name]


def _resp(status, body):
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def _key(run_id, name):
    return f"runs/{run_id}/{name}"


def _put_json(aws, bucket, run_id, name, obj):
    aws["s3"].put_object(Bucket=bucket, Key=_key(run_id, name), Body=json.dumps(obj).encode(),
                         ContentType="application/json")


def _decision_view(d):
    return {"lead_id": d.lead_id, "decision": d.decision, "reason": d.reason, "confidence": d.confidence,
            "decided_by": d.decided_by, "route_to": d.route_to, "flags": d.flags,
            "duplicate_of": d.duplicate_of, "manipulation_detected": d.manipulation_detected}


# ---------------------------------------------------------------- API

def api_handler(event, context=None, aws=None):
    aws = aws or _aws()
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    supplied, expected = headers.get("x-api-key", ""), _param(aws, "PARAM_API_KEY")
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        return _resp(401, {"error": "missing or invalid x-api-key"})
    route = event.get("routeKey", "")
    if route == "POST /runs":
        return _start_run(event, aws)
    if route == "GET /runs/{id}":
        return _get_run((event.get("pathParameters") or {}).get("id", ""), aws)
    return _resp(404, {"error": "not found"})


def _start_run(event, aws):
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        body = base64.b64decode(body).decode("utf-8", "replace")
    if len(body.encode()) > MAX_BYTES * 2:
        return _resp(413, {"error": f"request too large (limit {MAX_BYTES} bytes of CSV)"})
    try:
        csv_text = json.loads(body)["csv"]
        if not isinstance(csv_text, str):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        return _resp(400, {"error": 'body must be JSON like {"csv": "<file contents>"}'})
    if len(csv_text.encode()) > MAX_BYTES:
        return _resp(413, {"error": f"CSV too large (limit {MAX_BYTES} bytes)"})

    run_id = uuid.uuid4().hex[:12]
    tmp = Path(tempfile.mkdtemp()) / "input.csv"
    tmp.write_text(csv_text, encoding="utf-8", newline="")
    try:
        rows, digest = read_leads(tmp)
    except (SchemaError, ValueError, UnicodeError) as e:
        return _resp(400, {"error": f"invalid CSV: {e}"})
    finally:
        shutil.rmtree(tmp.parent, ignore_errors=True)
    if not rows:
        return _resp(400, {"error": "CSV has no rows"})
    if len(rows) > MAX_ROWS:
        return _resp(400, {"error": f"too many rows ({len(rows)}); limit is {MAX_ROWS}"})

    bucket = os.environ["BUCKET"]
    aws["s3"].put_object(Bucket=bucket, Key=_key(run_id, "input.csv"), Body=csv_text.encode(), ContentType="text/csv")
    progress = {"run_id": run_id, "status": "queued", "total": len(rows), "done": 0,
                "fixture_sha256": digest, "decisions": [], "created_at": time.time()}
    _put_json(aws, bucket, run_id, "progress.json", progress)
    aws["lam"].invoke(FunctionName=os.environ["WORKER_FUNCTION"], InvocationType="Event",
                      Payload=json.dumps({"run_id": run_id}).encode())
    return _resp(202, {"run_id": run_id, "status": "queued", "total": len(rows), "fixture_sha256": digest})


def _get_run(run_id, aws):
    if not RUN_ID_RE.match(run_id):
        return _resp(400, {"error": "invalid run id"})
    bucket = os.environ["BUCKET"]
    try:
        obj = aws["s3"].get_object(Bucket=bucket, Key=_key(run_id, "progress.json"))
        progress = json.loads(obj["Body"].read())
    except Exception as e:
        if type(e).__name__ == "NoSuchKey":
            return _resp(404, {"error": "run not found"})
        raise
    if progress.get("status") == "done":
        progress["downloads"] = {
            name: aws["s3"].generate_presigned_url(
                "get_object", Params={"Bucket": bucket, "Key": _key(run_id, name)}, ExpiresIn=PRESIGN_SECONDS)
            for name in DOWNLOADS}
    return _resp(200, progress)


# ---------------------------------------------------------------- worker

def worker_handler(event, context=None, aws=None, classifier=None, verifier=None):
    """Never raises: a failure is written to progress.json so the page can show it
    (an exception would make Lambda retry an asynchronous invocation)."""
    aws = aws or _aws()
    run_id = event["run_id"]
    bucket = os.environ["BUCKET"]
    work = Path(tempfile.mkdtemp())
    try:
        csv_bytes = aws["s3"].get_object(Bucket=bucket, Key=_key(run_id, "input.csv"))["Body"].read()
        input_path = work / "input.csv"
        input_path.write_bytes(csv_bytes)
        out_dir = work / "out"

        model = os.environ.get("OPENROUTER_MODEL", "")
        if classifier is None:
            classifier = OpenRouterClassifier(_param(aws, "PARAM_OPENROUTER"), model)
        if verifier is None and os.environ.get("PARAM_SERPAPI"):
            verifier = Verifier(
                SerpApiClient(_param(aws, "PARAM_SERPAPI"), cache_dir=work / "cache",
                              max_live_calls=int(os.environ.get("MAX_SEARCHES", "25"))),
                evidence_dir=out_dir / "search_evidence")

        views = []
        state = {"run_id": run_id, "status": "running", "decisions": views, "started_at": time.time()}

        def on_row(done, total, d):
            views.append(_decision_view(d))
            _put_json(aws, bucket, run_id, "progress.json", {**state, "done": done, "total": total})

        rows, digest = read_leads(input_path)
        state.update(total=len(rows), fixture_sha256=digest)
        _put_json(aws, bucket, run_id, "progress.json", {**state, "done": 0})
        mode = "online" if isinstance(classifier, OpenRouterClassifier) else "offline"
        decisions, digest, tool_run_id = run(input_path, out_dir, classifier, mode, model,
                                            delay=float(os.environ.get("ROW_DELAY", "0.5")),
                                            log=lambda *_: None, verifier=verifier, on_row=on_row)
        for f in out_dir.rglob("*"):
            if f.is_file():
                aws["s3"].put_object(Bucket=bucket, Key=_key(run_id, f.relative_to(out_dir).as_posix()),
                                     Body=f.read_bytes())
        counts = {}
        for d in decisions:
            counts[d.decision] = counts.get(d.decision, 0) + 1
        _put_json(aws, bucket, run_id, "progress.json",
                  {**state, "status": "done", "done": len(decisions), "total": len(decisions),
                   "counts": counts, "pipeline_run_id": tool_run_id, "finished_at": time.time()})
    except Exception as e:  # noqa: BLE001
        msg = f"{type(e).__name__}: {e}"[:300]
        try:
            _put_json(aws, bucket, run_id, "progress.json",
                      {"run_id": run_id, "status": "failed", "error": msg, "done": 0, "total": 0, "decisions": []})
        except Exception:  # noqa: BLE001
            pass
    finally:
        shutil.rmtree(work, ignore_errors=True)
