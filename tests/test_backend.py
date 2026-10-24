import json
import os
import unittest
from pathlib import Path

from backend import handler
from triage.classifier import OfflineClassifier

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "inbound_leads.csv"
API_KEY = "test-api-key"


class NoSuchKey(Exception):
    pass


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[(Bucket, Key)] = Body

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise NoSuchKey(Key)

        class B:
            def __init__(s, b):
                s.b = b

            def read(s):
                return s.b
        return {"Body": B(self.objects[(Bucket, Key)])}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://signed.example/{Params['Key']}?exp={ExpiresIn}"


class FakeSSM:
    def get_parameter(self, Name, WithDecryption):
        return {"Parameter": {"Value": API_KEY}}


class FakeLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, **kw):
        self.calls.append(kw)


def make_aws():
    return {"s3": FakeS3(), "ssm": FakeSSM(), "lam": FakeLambda()}


def post(csv_text, key=API_KEY, raw_body=None):
    body = raw_body if raw_body is not None else json.dumps({"csv": csv_text})
    return {"routeKey": "POST /runs", "headers": {"X-Api-Key": key}, "body": body}


def get(run_id, key=API_KEY):
    return {"routeKey": "GET /runs/{id}", "headers": {"x-api-key": key}, "pathParameters": {"id": run_id}}


class BackendCase(unittest.TestCase):
    def setUp(self):
        handler._param_cache.clear()
        os.environ.update(BUCKET="b", WORKER_FUNCTION="worker", PARAM_API_KEY="/p/api", ROW_DELAY="0")
        os.environ.pop("PARAM_SERPAPI", None)
        self.aws = make_aws()
        self.csv = FIXTURE.read_text(encoding="utf-8")

    def call(self, event):
        r = handler.api_handler(event, None, aws=self.aws)
        return r["statusCode"], json.loads(r["body"])


class ApiTests(BackendCase):
    def test_wrong_or_missing_key_is_401_and_nothing_is_stored(self):
        for k in ("", "nope"):
            self.assertEqual(self.call(post(self.csv, key=k))[0], 401)
        self.assertEqual(self.aws["s3"].objects, {})
        self.assertEqual(self.aws["lam"].calls, [])

    def test_start_run_stores_input_and_invokes_worker_async(self):
        status, body = self.call(post(self.csv))
        self.assertEqual(status, 202)
        self.assertEqual((body["status"], body["total"]), ("queued", 20))
        self.assertEqual(body["fixture_sha256"], "cc1927ca771c37b186a2abdb7b9757594da79ec6dda074e5a514dd2761cc8599")
        rid = body["run_id"]
        self.assertIn(("b", f"runs/{rid}/input.csv"), self.aws["s3"].objects)
        call = self.aws["lam"].calls[0]
        self.assertEqual((call["FunctionName"], call["InvocationType"]), ("worker", "Event"))
        self.assertEqual(json.loads(call["Payload"]), {"run_id": rid})

    def test_bad_inputs(self):
        self.assertEqual(self.call(post(None, raw_body="not json"))[0], 400)
        self.assertEqual(self.call(post(None, raw_body=json.dumps({"csv": 5})))[0], 400)
        self.assertEqual(self.call(post("lead_id,email\nL-1,a@b.co\n"))[0], 400)  # schema
        header = self.csv.splitlines()[0]
        self.assertEqual(self.call(post(header + "\n"))[0], 400)  # no rows
        self.assertEqual(self.call(post("x" * (handler.MAX_BYTES + 1)))[0], 413)
        many = header + "\n" + "\n".join(f"L-{i},2026-06-01T09:00:00Z,n,e@x.co,c,https://x.co,100,m,s"
                                         for i in range(handler.MAX_ROWS + 1))
        self.assertEqual(self.call(post(many))[0], 400)
        self.assertEqual(self.aws["lam"].calls, [])

    def test_get_validates_id_and_404s(self):
        self.assertEqual(self.call(get("../etc/passwd"))[0], 400)
        self.assertEqual(self.call(get("zzzzzzzzzzzz"))[0], 400)
        self.assertEqual(self.call(get("abcdef012345"))[0], 404)

    def test_unknown_route(self):
        self.assertEqual(self.call({"routeKey": "DELETE /runs", "headers": {"x-api-key": API_KEY}})[0], 404)


class WorkerTests(BackendCase):
    def run_worker(self, **kw):
        _, body = self.call(post(self.csv))
        handler.worker_handler({"run_id": body["run_id"]}, None, aws=self.aws, **kw)
        return body["run_id"]

    def progress(self, rid):
        return json.loads(self.aws["s3"].objects[("b", f"runs/{rid}/progress.json")])

    def test_full_run_writes_outputs_and_done_progress(self):
        rid = self.run_worker(classifier=OfflineClassifier())
        p = self.progress(rid)
        self.assertEqual((p["status"], p["done"], p["total"]), ("done", 20, 20))
        self.assertEqual(len(p["decisions"]), 20)
        self.assertEqual(sum(p["counts"].values()), 20)
        for name in ("decisions.csv", "decisions.json", "run_log.jsonl"):
            self.assertIn(("b", f"runs/{rid}/{name}"), self.aws["s3"].objects)

    def test_get_done_run_returns_presigned_downloads(self):
        rid = self.run_worker(classifier=OfflineClassifier())
        status, body = self.call(get(rid))
        self.assertEqual(status, 200)
        self.assertEqual(set(body["downloads"]), set(handler.DOWNLOADS))
        self.assertTrue(all(u.startswith("https://signed.example/") for u in body["downloads"].values()))

    def test_running_progress_is_incremental(self):
        seen = []
        real_put = self.aws["s3"].put_object

        def spy(Bucket, Key, Body, **kw):
            if Key.endswith("progress.json"):
                seen.append(json.loads(Body)["done"])
            real_put(Bucket=Bucket, Key=Key, Body=Body, **kw)
        self.aws["s3"].put_object = spy
        self.run_worker(classifier=OfflineClassifier())
        self.assertEqual(sorted(seen), seen)
        self.assertGreater(len(set(seen)), 5)

    def test_worker_failure_is_recorded_not_raised(self):
        class Boom:
            offers_tools = False

            def classify(self, a):
                raise RuntimeError("secret-looking detail " + "x" * 500)

        rid = self.run_worker(classifier=Boom())
        p = self.progress(rid)
        # per-row classifier errors become ESCALATE rows, so the run still completes
        self.assertEqual(p["status"], "done")
        self.assertTrue(all(d["decision"] in {"ESCALATE", "REJECT", "NURTURE"} for d in p["decisions"]))

    def test_missing_input_marks_run_failed_without_raising(self):
        handler.worker_handler({"run_id": "abcdef012345"}, None, aws=self.aws, classifier=OfflineClassifier())
        p = self.progress("abcdef012345")
        self.assertEqual(p["status"], "failed")
        self.assertLessEqual(len(p["error"]), 300)

    def test_secrets_are_not_in_outputs(self):
        rid = self.run_worker(classifier=OfflineClassifier())
        blob = b"".join(v for (b, k), v in self.aws["s3"].objects.items() if k.startswith(f"runs/{rid}/"))
        self.assertNotIn(API_KEY.encode(), blob)


if __name__ == "__main__":
    unittest.main()
