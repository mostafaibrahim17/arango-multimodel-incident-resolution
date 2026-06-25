"""Build the runbook knowledge graph with AutoGraph.

AutoGraph discovers the knowledge domains in the runbook corpus and assigns a retrieval strategy per
domain (FullGraphRAG for entity-rich content, VectorRAG for simpler content), then builds the graph.
Most of the pipeline is scriptable over the documented REST API; the final entity build is a UI click:

    health -> import-multiple (per module) -> corpus/builds -> rag-strategizer/analyze    [scriptable]
    -> orchestrate (the final entity build)                                                [UI step]

Two steps stay in the web UI (same pattern as deploying the Importer/Retriever): (1) create the
AutoGraph PROJECT once -- that deploys the `arangodb-autograph-<pf>` control service + the project's
retriever; (2) click "Continue to Import" to run orchestration. The REST POST /orchestrate returns
`{"totalJobs": 0}` and builds nothing on this platform version (verified on a clean slate), so the
final entity build is a UI click. Everything else (import, corpus build, the FullGraphRAG strategizer)
runs here. The control plane lives at `{HOST}/autograph/{postfix}/v1` (postfix discovered at runtime).

Usage:
  python graphrag_ingest.py          # verify if built; else import+corpus+strategize, then prompt for the UI step
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
    """Drop the project's AutoGraph KG (named graph + collections). Destructive -- our own project only."""
    db = _db()
    # Drop every named graph for this project FIRST (AutoGraph creates several -- _kg, _CorpusGraph,
    # ...); a collection can't be deleted while it's a vertex/edge in a graph. drop_collections=True
    # takes the graph's own collections with it; the loop below cleans up any non-graph collections.
    for graph in db.graphs():
        if graph["name"].startswith(PROJECT):
            db.delete_graph(graph["name"], drop_collections=True)
    for coll in sorted(c["name"] for c in db.collections() if c["name"].startswith(PROJECT)):
        db.delete_collection(coll)
    print(f"reset: dropped all {PROJECT} graphs + collections")


def _poll_collection(coll, max_s=1200, label=""):
    """Poll a collection count until it is non-zero and unchanged across 3 reads, then return it.

    The AutoGraph corpus-build / strategizer / orchestrate steps are all ASYNC and don't expose a
    reliable terminal-status field, so we watch the collections they populate instead.
    """
    db = _db()
    t0, prev, stable = time.time(), -1, 0
    while time.time() - t0 < max_s:
        n = db.collection(coll).count() if db.has_collection(coll) else 0
        print(f"  [{int(time.time() - t0)}s] {label or coll}: {n}", flush=True)
        stable = stable + 1 if (n > 0 and n == prev) else 0
        prev = n
        if stable >= 3:
            return n
        time.sleep(15)
    return prev


def import_runbooks(module="default"):
    """Import every data/runbooks/**/*.md into AutoGraph as ONE module.

    The validated build imports the whole corpus as a single module so the RAG Strategizer sees one
    incident-response domain and assigns it FullGraphRAG (1 partition -> 197 entities / 287 relations).
    Splitting into per-folder modules makes AutoGraph cluster each separately and can downgrade the
    smaller ones to VectorRAG -- not what we want here.
    """
    jwt = token()
    base = autograph_url(jwt=jwt)
    print(f"autograph: {requests.get(f'{base}/health', headers=_auth(jwt), timeout=20).json()}")
    files = sorted(glob.glob("data/runbooks/**/*.md", recursive=True))
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
    """Corpus build -> RAG strategizer (FullGraphRAG high) -> orchestrate. All three are ASYNC:
    each POST returns immediately ('started'); we poll the collections / strategy they populate."""
    jwt = token()
    base = autograph_url(jwt=jwt)

    # 1. Corpus build (async): clusters the imported docs into domains. Wait for the domains to settle.
    r = requests.post(f"{base}/corpus/builds", headers=_auth(jwt),
                      json={"embedding_strategy": "first_chunk", "strategy": {"top_k": 7, "cluster_threshold": 2}},
                      timeout=120)
    r.raise_for_status()
    print(f"corpus build started: {r.json()}", flush=True)
    _poll_collection(f"{PROJECT}_domains", label="corpus domains")

    # 2. RAG strategizer (async): assign a strategy per domain. Poll /strategy until it reports them.
    #    full_graph_rag_strategy=high biases entity-rich runbooks to FullGraphRAG (vs the lighter VectorRAG).
    requests.post(f"{base}/rag-strategizer/analyze", headers=_auth(jwt),
                  json={"full_graph_rag_strategy": "high"}, timeout=120).raise_for_status()
    t0 = time.time()
    while time.time() - t0 < 600:
        s = requests.get(f"{base}/rag-strategizer/strategy", headers=_auth(jwt), timeout=60).json()
        print(f"  [{int(time.time() - t0)}s] strategies: {s.get('totalStrategies', 0)} {s.get('strategyTypeCounts', {})}", flush=True)
        if s.get("totalStrategies", 0) > 0:
            break
        time.sleep(15)
    else:
        raise RuntimeError("RAG strategizer produced no strategies")

    # 3. Orchestrate (the final entity-build) is a UI step on this platform version.
    #    The REST POST /orchestrate returns `{"totalJobs": 0}` and spawns no workers (verified on a
    #    clean slate), so the knowledge graph never builds from the API. The web UI's "Continue to
    #    Import" button triggers it correctly. Everything up to here (import, corpus build, the
    #    FullGraphRAG strategy) is scriptable; this last click is not -- same pattern as deploying the
    #    services. So: open the AutoGraph project in the UI and click "Continue to Import", then re-run
    #    this script (no args) to verify the entities built.
    print("\nNEXT (UI): open the AutoGraph project in the web UI and click 'Continue to Import' to build\n"
          "the knowledge graph. The REST /orchestrate endpoint returns 0 jobs on this platform version.\n"
          "Then re-run `python graphrag_ingest.py` to verify.", flush=True)
    return "awaiting-ui-orchestrate"


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
