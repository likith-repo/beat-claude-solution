"""Streamlit front end for the lead triage API (POST /runs, then poll GET /runs/{id}).

Config comes from the environment or .streamlit/secrets.toml, never from the page:
  LEAD_TRIAGE_API_URL   e.g. https://xxlg3of1y3.execute-api.ap-southeast-1.amazonaws.com
  LEAD_TRIAGE_API_KEY   the value of the SSM parameter /lead-triage/api_key
"""
import os
import time

import pandas as pd
import requests
import streamlit as st

POLL_SECONDS = 2.5
MAX_WAIT_SECONDS = 900
BRIEF_VERSION = "2026-07"  # mirrors the brief_version written to the run log
MAX_BYTES = 100_000  # mirrors backend.handler.MAX_BYTES
DECISION_ORDER = ["QUALIFY", "NURTURE", "REJECT", "ESCALATE"]
COLUMNS = ["lead_id", "decision", "confidence", "reason", "route_to", "decided_by", "duplicate_of", "flags"]


def setting(name):
    value = os.environ.get(name)
    if value:
        return value
    try:
        return st.secrets[name]
    except (KeyError, FileNotFoundError):
        return None


def call(method, path, key, base, **kw):
    r = requests.request(method, f"{base.rstrip('/')}{path}", headers={"x-api-key": key}, timeout=30, **kw)
    try:
        body = r.json()
    except ValueError:
        body = {"error": r.text[:200]}
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}: {body.get('error', body)}")
    return body


def safe_cell(v):
    """Defuse spreadsheet formulas in exported text (lead_id and reason can carry lead text)."""
    return "'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r") else v


def table(decisions):
    df = pd.DataFrame(decisions)
    for col in COLUMNS:
        if col not in df:
            df[col] = None
    df["flags"] = df["flags"].apply(lambda f: ", ".join(f) if isinstance(f, list) else f)
    return df[COLUMNS]


st.set_page_config(page_title="Beat Claude Challenge | Lead Triage", page_icon=":material/target:", layout="wide")
with st.container(border=True):
    st.markdown("##### BEAT CLAUDE CHALLENGE")
    st.markdown("**Topic:** AI Automation Intern 012, The Lead Triage Agent")
    st.caption(f"Brief version {BRIEF_VERSION}")
st.title("Lead Triage Agent")
st.caption("Upload a leads CSV. Each row is classified QUALIFY, NURTURE, REJECT or ESCALATE. "
           "The result is a first pass for a human to review, not a final call.")

base, key = setting("LEAD_TRIAGE_API_URL"), setting("LEAD_TRIAGE_API_KEY")
if not (base and key):
    st.error("Set LEAD_TRIAGE_API_URL and LEAD_TRIAGE_API_KEY (environment or .streamlit/secrets.toml).")
    st.stop()

upload = st.file_uploader("Leads CSV", type="csv")
if upload and st.button("Run triage", type="primary"):
    raw = upload.getvalue()
    if len(raw) > MAX_BYTES:
        st.error(f"File is {len(raw):,} bytes; the limit is {MAX_BYTES:,}.")
        st.stop()
    try:
        started = call("POST", "/runs", key, base, json={"csv": raw.decode("utf-8")})
    except (RuntimeError, UnicodeDecodeError, requests.RequestException) as e:
        st.error(f"Could not start the run: {e}")
        st.stop()
    st.session_state["run"] = started

run = st.session_state.get("run")
if run:
    st.write(f"Run `{run['run_id']}` · {run['total']} rows · fixture SHA-256 `{run['fixture_sha256']}`")
    bar, status, view = st.progress(0.0), st.empty(), st.empty()
    deadline = time.monotonic() + MAX_WAIT_SECONDS
    progress = {}
    while time.monotonic() < deadline:
        try:
            progress = call("GET", f"/runs/{run['run_id']}", key, base)
        except (RuntimeError, requests.RequestException) as e:
            status.error(f"Lost contact with the API: {e}")
            st.stop()
        total = progress.get("total") or run["total"]
        bar.progress(min(progress.get("done", 0) / total, 1.0))
        status.write(f"Status: **{progress['status']}** · {progress.get('done', 0)}/{total}")
        if progress.get("decisions"):
            view.dataframe(table(progress["decisions"]), use_container_width=True, hide_index=True)
        if progress["status"] in ("done", "failed"):
            break
        time.sleep(POLL_SECONDS)
    else:
        status.warning("Still running after 15 minutes. Reload and the run will be shown again if it finishes.")
        st.stop()

    if progress["status"] == "failed":
        st.error(f"Run failed: {progress.get('error', 'unknown error')}")
    else:
        counts = progress.get("counts", {})
        cols = st.columns(len(DECISION_ORDER))
        for col, name in zip(cols, DECISION_ORDER):
            col.metric(name, counts.get(name, 0))
        st.subheader("Downloads")
        st.caption("Links expire after 15 minutes. Run again or reload to get fresh ones.")
        for name, url in progress.get("downloads", {}).items():
            st.link_button(name, url)
        st.download_button("decisions (table view, CSV)",
                           table(progress["decisions"]).apply(lambda col: col.map(safe_cell)).to_csv(index=False),
                           file_name=f"triage_{run['run_id']}.csv", mime="text/csv")
