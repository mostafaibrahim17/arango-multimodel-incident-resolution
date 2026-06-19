"""Run the whole pipeline end to end, the same logic the notebook walks through.

  1. Multimodel core  -> incidents + alerts + service topology in Arango
  2. GraphRAG KG       -> runbooks imported into a knowledge graph (skip-if-built)
  3. Combined resolve  -> one alert -> structured payload + cited, grounded next-step

Run:  python run_all.py [alert.json]
"""
import json
import sys

import ingest
import graphrag_ingest
from resolver import answer, corroborated, resolve


def main():
    print("== 1. Multimodel core (incidents + alerts + topology) ==")
    ingest.main()

    print("\n== 2. GraphRAG knowledge graph (runbooks) ==")
    graphrag_ingest.main()  # skip-if-built; pass --reset to rebuild

    print("\n== 3. Combined resolution ==")
    path = sys.argv[1] if len(sys.argv) > 1 else "data/alert.sample.json"
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
