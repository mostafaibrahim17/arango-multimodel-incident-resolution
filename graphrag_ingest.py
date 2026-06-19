"""Build the runbook knowledge graph via the GraphRAG Importer (scriptable + reproducible).

A reader can run this end to end. On this platform AutoGraph is a guided UI wizard (it creates
its own project and deploys its own services, and is not exposed as a scriptable data-plane API
here), so the reproducible build uses the Importer directly with rag_mode=full_graphrag.
AutoGraph's automatic per-domain treatment is shown as a UI section -- see the README.

Usage:
  python graphrag_ingest.py          # build if not already built (skip-if-built)
  python graphrag_ingest.py --reset  # drop the project KG and rebuild from scratch
"""
import base64
import glob
import os
import sys
import time

import requests
from arango import ArangoClient
from dotenv import load_dotenv

from graphrag import importer_url, token

load_dotenv()

HOST = os.environ["ARANGO_HOST"].rstrip("/")
GDB = os.environ.get("GRAPHRAG_DB", "_system")
PROJECT = os.environ.get("GRAPHRAG_PROJECT", "test-incident-demo")
U = os.environ["ARANGO_USER"]
PWD = os.environ["ARANGO_PASSWORD"]
RUNBOOK_BASE_URL = "https://runbooks.internal"
_KG_SUFFIXES = ["Documents", "Chunks", "Entities", "Communities", "Relations", "SemanticUnits"]


def _db():
    return ArangoClient(hosts=HOST).db(GDB, username=U, password=PWD)


def kg_built():
    db = _db()
    e = f"{PROJECT}_Entities"
    return db.has_collection(e) and db.collection(e).count() > 0


def reset_kg():
    """Drop the project's KG collections + named graph. Destructive -- our own project only."""
    db = _db()
    if db.has_graph(f"{PROJECT}_kg"):
        db.delete_graph(f"{PROJECT}_kg")
    for s in _KG_SUFFIXES:
        c = f"{PROJECT}_{s}"
        if db.has_collection(c):
            db.delete_collection(c)
    print(f"reset: dropped {PROJECT}_* KG collections + graph")


def _poll(base, job, jwt):
    last, t0 = None, time.time()
    while time.time() - t0 < 900:
        s = requests.get(f"{base}/jobs/{job}", headers={"Authorization": f"Bearer {jwt}"}, timeout=20).json().get("job", {})
        cs = s.get("currentStatus", {})
        st = cs.get("status") if isinstance(cs, dict) else cs
        if st != last:
            msg = cs.get("message", "") if isinstance(cs, dict) else ""
            print(f"  [{int(time.time() - t0)}s] {st}: {msg[:110]}")
            last = st
        if s.get("isTerminal"):
            return st
        time.sleep(12)
    return "timeout"


def import_runbooks(retry=1):
    """Import every runbooks/**/*.md into the KG (one full_graphrag job). Retries a transient fail."""
    jwt = token()
    base = importer_url(jwt=jwt)
    files = sorted(glob.glob("runbooks/**/*.md", recursive=True))
    payload = {
        "files": [
            {"name": os.path.basename(f),
             "content": base64.b64encode(open(f, "rb").read()).decode(),
             "citable_url": f"{RUNBOOK_BASE_URL}/{os.path.basename(f)[:-3]}"}
            for f in files
        ],
        "rag_mode": "full_graphrag",
        "chunk_token_size": 1024,
        "enable_chunk_embeddings": True,
        "enable_community_embeddings": True,
    }
    st = None
    for attempt in range(retry + 1):
        r = requests.post(f"{base}/import-multiple",
                          headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
                          json=payload, timeout=120)
        r.raise_for_status()
        job = r.json().get("jobId")
        print(f"import submitted ({len(files)} runbooks): job {job}")
        st = _poll(base, job, jwt)
        if st == "service_completed":
            return st
        print(f"  attempt {attempt + 1} ended in {st}" + ("; retrying (transient)..." if attempt < retry else "; giving up"))
    return st


def verify_kg():
    db = _db()
    print("KG verification:")
    counts = {}
    for s in ["Documents", "Chunks", "Entities", "Communities", "Relations"]:
        n = db.collection(f"{PROJECT}_{s}").count() if db.has_collection(f"{PROJECT}_{s}") else 0
        counts[s] = n
        print(f"  {PROJECT}_{s}: {n}")
    ok = db.has_graph(f"{PROJECT}_kg") and counts["Entities"] > 0 and counts["Relations"] > 0
    print(f"  graph {PROJECT}_kg: {db.has_graph(f'{PROJECT}_kg')} | PASS: {ok}")
    return ok


def main():
    reset = "--reset" in sys.argv
    if reset:
        reset_kg()
    if kg_built() and not reset:
        print("KG already built (skip; pass --reset to rebuild).")
        verify_kg()
        return
    import_runbooks(retry=1)
    verify_kg()


if __name__ == "__main__":
    main()
