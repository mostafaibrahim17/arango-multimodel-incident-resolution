"""Build the runbook knowledge graph with AutoGraph (scriptable, via its HTTP REST API).

A reader can run this end to end. AutoGraph discovers the knowledge domains in the runbook
corpus and assigns a retrieval strategy per domain (FullGraphRAG for entity-rich content,
VectorRAG for simpler content), then builds the graph. It is driven entirely over the documented
REST API -- no UI clicks for the build itself:

    health -> import-multiple (per module) -> corpus/builds -> rag-strategizer/analyze -> orchestrate

The one manual prerequisite (same as the Importer/Retriever before it) is creating the AutoGraph
PROJECT once in the web UI; that deploys the `arangodb-autograph-<pf>` control service and the
project's retriever. Everything after that is scriptable here. The control plane lives at
`{HOST}/autograph/{postfix}/v1` (postfix discovered at runtime from the serviceId, never hardcoded).

Usage:
  python graphrag_ingest.py          # build if not already built (skip-if-built)
  python graphrag_ingest.py --reset  # drop the project KG collections and rebuild from scratch
"""
import base64
import glob
import os
import sys
import time

import requests
from arango import ArangoClient
from dotenv import load_dotenv

from graphrag import autograph_url, token

load_dotenv()

HOST = os.environ["ARANGO_HOST"].rstrip("/")
GDB = os.environ.get("GRAPHRAG_DB", "incident_demo")
PROJECT = os.environ.get("GRAPHRAG_PROJECT", "incident-runbook-autograph")
U = os.environ["ARANGO_USER"]
PWD = os.environ["ARANGO_PASSWORD"]
RUNBOOK_BASE_URL = "https://runbooks.internal"
# AutoGraph KG collections (in the incident_demo database, prefixed by the project name)
_KG_SUFFIXES = ["Documents", "Chunks", "Entities", "Communities", "Relations",
                "corpus_relations", "domains", "modules", "rags", "similarities", "sources"]


def _db():
    return ArangoClient(hosts=HOST).db(GDB, username=U, password=PWD)


def _auth(jwt):
    return {"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"}


def kg_built():
    db = _db()
    entities = f"{PROJECT}_Entities"
    return db.has_collection(entities) and db.collection(entities).count() > 0


def reset_kg():
    """Drop the project's AutoGraph KG collections. Destructive -- our own project only."""
    db = _db()
    for suffix in _KG_SUFFIXES:
        coll = f"{PROJECT}_{suffix}"
        if db.has_collection(coll):
            db.delete_collection(coll)
    print(f"reset: dropped {PROJECT}_* AutoGraph KG collections")


def _poll_corpus(base, build_id, jwt):
    """Poll a corpus build to terminal status (GET /v1/corpus/builds/{id})."""
    last, t0 = None, time.time()
    while time.time() - t0 < 1200:
        body = requests.get(f"{base}/corpus/builds/{build_id}", headers=_auth(jwt), timeout=20).json()
        status = body.get("status") or body.get("currentStatus")
        if status != last:
            print(f"  [{int(time.time() - t0)}s] corpus build: {status}")
            last = status
        if status in ("completed", "failed", "error"):
            return status
        time.sleep(12)
    return "timeout"


def import_runbooks():
    """Import every data/runbooks/<module>/*.md into AutoGraph, one call per module folder."""
    jwt = token()
    base = autograph_url(jwt=jwt)
    print(f"autograph: {requests.get(f'{base}/health', headers=_auth(jwt), timeout=20).json()}")
    modules = {}
    for path in sorted(glob.glob("data/runbooks/**/*.md", recursive=True)):
        module = os.path.basename(os.path.dirname(path))  # service-family folder = AutoGraph module
        modules.setdefault(module, []).append(path)
    for module, files in modules.items():
        payload = {
            "module": module,
            "files": [
                {"doc_name": os.path.basename(f),
                 "content": base64.b64encode(open(f, "rb").read()).decode(),
                 "citable_url": f"{RUNBOOK_BASE_URL}/{os.path.basename(f)[:-3]}"}
                for f in files
            ],
        }
        r = requests.post(f"{base}/import-multiple", headers=_auth(jwt), json=payload, timeout=120)
        r.raise_for_status()
        print(f"  imported module {module!r}: {len(files)} file(s)")


def build_corpus():
    """Start the corpus build, then assign per-domain RAG strategies (FullGraphRAG high), then orchestrate."""
    jwt = token()
    base = autograph_url(jwt=jwt)

    r = requests.post(f"{base}/corpus/builds", headers=_auth(jwt),
                      json={"embedding_strategy": "first_chunk", "strategy": {"top_k": 7, "cluster_threshold": 2}},
                      timeout=120)
    r.raise_for_status()
    build_id = r.json().get("corpus_build_id") or r.json().get("id")
    print(f"corpus build started: {build_id}")
    status = _poll_corpus(base, build_id, jwt)
    print(f"corpus build: {status}")
    if status != "completed":
        return status

    # Bias the strategizer toward FullGraphRAG so entity-rich runbooks get a real knowledge graph.
    requests.post(f"{base}/rag-strategizer/analyze", headers=_auth(jwt),
                  json={"full_graph_rag_strategy": "high"}, timeout=300).raise_for_status()
    strategy = requests.get(f"{base}/rag-strategizer/strategy", headers=_auth(jwt), timeout=60).json()
    print(f"rag strategies: {strategy}")

    # Spawn the importer workers that build the knowledge graph for each FullGraphRAG partition.
    r = requests.post(f"{base}/orchestrate", headers=_auth(jwt),
                      json={"replicas": 2, "max_retries": 3}, timeout=120)
    r.raise_for_status()
    print(f"orchestrate: {r.json()}")
    return "completed"


def verify_kg():
    db = _db()
    print("KG verification:")
    counts = {}
    for suffix in ["Documents", "Chunks", "Entities", "Communities", "Relations"]:
        coll = f"{PROJECT}_{suffix}"
        counts[suffix] = db.collection(coll).count() if db.has_collection(coll) else 0
        print(f"  {coll}: {counts[suffix]}")
    ok = counts["Entities"] > 0 and counts["Relations"] > 0 and counts["Documents"] > 0
    print(f"  PASS: {ok}")
    return ok


def main():
    reset = "--reset" in sys.argv
    if reset:
        reset_kg()
    if kg_built() and not reset:
        print("KG already built (skip; pass --reset to rebuild).")
        verify_kg()
        return
    import_runbooks()
    build_corpus()
    verify_kg()


if __name__ == "__main__":
    main()
