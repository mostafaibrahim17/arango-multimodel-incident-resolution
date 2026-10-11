"""Run the whole pipeline end to end, the same logic the notebook walks through.

  1. Multimodel core  -> incidents + alerts + service topology in Arango
  2. AutoGraph KG      -> runbooks built into a knowledge graph (verify-only here)
  3. Combined resolve  -> one alert -> structured payload + cited, grounded next-step

By default this is non-destructive: it verifies each store is already in place and goes straight to
resolution. It does NOT rebuild the operational store or re-import the knowledge graph on every run.

Run:
  python run_all.py [alert.json]        # verify both stores, then resolve
  python run_all.py --rebuild [alert.json]   # reload the operational store first (drops + re-embeds)
"""
import json
import sys

import ingest
import graphrag_ingest
from resolver import answer, corroborated, resolve


def _operational_store_ready():
    """True when the operational store already holds incidents, so a rebuild is not needed."""
    try:
        db = ingest.connect()
        return db.has_collection("incidents") and db.collection("incidents").count() > 0
    except Exception:
        return False


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    rebuild = "--rebuild" in sys.argv

    print("== 1. Multimodel core (incidents + alerts + topology) ==")
    if rebuild or not _operational_store_ready():
        ingest.main()
    else:
        print("operational store already built (skip; pass --rebuild to reload it).")

    print("\n== 2. AutoGraph knowledge graph (runbooks) ==")
    # Verify only. The knowledge graph is built once (see graphrag_ingest.py: the final build is a
    # UI step), so run_all never re-imports it. If it is not built yet, say so and continue; the
    # resolver degrades to the pinned runbook when the retriever is unavailable.
    if graphrag_ingest.kg_built():
        graphrag_ingest.verify_kg()
    else:
        print("knowledge graph not built yet. Build it with:\n"
              "  python src/graphrag_ingest.py   (then finish the 'Continue to build' step in the UI)\n"
              "Continuing; the cited answer will fall back to the pinned runbook until it is built.")

    print("\n== 3. Combined resolution ==")
    path = args[0] if args else "data/alert.sample.json"
    alert = json.load(open(path))
    payload = resolve(alert)
    cited = answer(payload, alert.get("text"))
    print(json.dumps({
        "structured": payload,
        "cited_answer": cited,
        "corroboration": corroborated(payload, cited),
    }, indent=2))


if __name__ == "__main__":
    main()