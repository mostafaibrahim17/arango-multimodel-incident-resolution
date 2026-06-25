"""Render the figures for the README + notebook from real data. Saves PNGs to assets/.

  assets/architecture.png      -- the two-surfaces-one-platform schematic
  assets/affected-subgraph.png -- the real affected-service subgraph for the sample alert
  assets/knowledge-graph.png   -- a sample of the AutoGraph knowledge graph (entities + relations)
"""
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import networkx as nx
from matplotlib.patches import FancyBboxPatch

from graphrag import GRAPHRAG_PROJECT, kg_db

os.makedirs("assets", exist_ok=True)
GREEN, TEAL, NAVY, AMBER = "#7cc24a", "#3aa6a6", "#1f3a5f", "#e08a3c"


def _box(ax, x, y, w, h, text, fc, fs=10, tc="white"):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.06",
                                fc=fc, ec="none"))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", color=tc, fontsize=fs, wrap=True)


def fig_architecture():
    fig, ax = plt.subplots(figsize=(11, 5.2))
    ax.set_xlim(0, 11)
    ax.set_ylim(0, 5.2)
    ax.axis("off")
    _box(ax, 0.2, 2.1, 1.6, 1.0, "Live alert\n(JSON)", AMBER, 10)
    _box(ax, 2.3, 0.3, 4.4, 4.6, "Arango Contextual Data Platform", NAVY, 12, "white")
    _box(ax, 2.6, 2.7, 3.8, 1.8, "Multimodel core  (incident_demo)\n\nincidents + vector index\nservice topology (graph)\nteams (key-value) + alerts", TEAL, 9)
    _box(ax, 2.6, 0.6, 3.8, 1.8, "AutoGraph knowledge graph\n(runbooks)\n\nrunbooks -> entities,\nrelations, communities", GREEN, 9)
    _box(ax, 7.2, 2.1, 1.7, 1.0, "Agent\n(resolver.py)", "#444", 10)
    _box(ax, 9.1, 3.0, 1.7, 1.6, "Structured payload\nsimilar + affected\n+ on-call", TEAL, 8)
    _box(ax, 9.1, 0.6, 1.7, 1.6, "Cited answer\ngrounded in\nrunbooks", GREEN, 8)
    for x0, y0, x1, y1 in [(1.8, 2.6, 2.6, 2.6), (6.4, 3.4, 7.2, 2.8), (6.4, 1.4, 7.2, 2.4),
                           (8.9, 2.8, 9.1, 3.6), (8.9, 2.4, 9.1, 1.4)]:
        ax.annotate("", xy=(x1, y1), xytext=(x0, y0), arrowprops=dict(arrowstyle="-|>", color="#888", lw=1.6))
    ax.text(5.5, 4.95, "one alert in -> one AQL round trip (vector + graph + key-value) + grounded, cited answer",
            ha="center", fontsize=9, style="italic", color="#555")
    plt.tight_layout()
    plt.savefig("assets/architecture-schematic.png", dpi=130, bbox_inches="tight")
    plt.close()
    print("wrote assets/architecture-schematic.png")


def fig_affected_subgraph(alert_path="data/alert.sample.json"):
    from resolver import resolve
    payload = resolve(json.load(open(alert_path)))
    affected = {a["service"]: a for a in payload["affected_services"]}
    topo = json.load(open("data/topology.json"))
    names = {k: v["name"] for k, v in topo["services"].items()}
    G = nx.DiGraph()
    for a in affected:
        G.add_node(a)
    for fr, to in topo["depends_on"]:
        if fr in affected and to in affected:
            G.add_edge(fr, to)
    depth = {k: v["depth"] for k, v in affected.items()}
    pos = nx.spring_layout(G, seed=7, k=1.2)
    colors = [["#d94e3a", TEAL, GREEN, "#9ccb6a"][min(depth[n], 3)] for n in G.nodes]
    plt.figure(figsize=(9, 6))
    nx.draw_networkx_edges(G, pos, edge_color="#bbb", arrows=True, arrowsize=14, width=1.4)
    nx.draw_networkx_nodes(G, pos, node_color=colors, node_size=2200, edgecolors="white")
    nx.draw_networkx_labels(G, pos, {n: names.get(n, n) for n in G.nodes}, font_size=8)
    plt.title(f"Affected-service subgraph for an alert on {payload['root_service']}  "
              f"(root in red, by blast-radius depth)", fontsize=11)
    plt.axis("off")
    plt.tight_layout()
    plt.savefig("assets/affected-subgraph.png", dpi=130, bbox_inches="tight")
    plt.close()
    print("wrote assets/affected-subgraph.png")


def fig_kg_sample():
    """The real AutoGraph KG: entities (MENTIONED_IN chunks, PART_OF documents) clustered by
    runbook, with the few direct RELATED_TO entity-entity edges overlaid. Runbooks are hubs."""
    import re
    db = kg_db()
    p = GRAPHRAG_PROJECT
    # entity -> chunk (MENTIONED_IN) -> document (PART_OF). Label each document by the service in
    # its content header (`# Runbook: <svc>`), since AutoGraph's Document.file_name can be misaligned.
    rows = list(db.aql.execute(f"""
        FOR r IN `{p}_Relations` FILTER r.type == 'MENTIONED_IN'
          LET e = DOCUMENT(r._from).entity_name
          LET doc = FIRST(FOR pr IN `{p}_Relations` FILTER pr._from == r._to AND pr.type == 'PART_OF'
                          RETURN DOCUMENT(pr._to).content)
          FILTER e != null AND doc != null
          RETURN {{e: e, doc: doc}}"""))
    def _svc(content):
        m = re.search(r'#\s*Runbook:\s*([a-z0-9-]+)', content or "")
        return m.group(1) if m else "runbook"
    for r in rows:
        r["doc"] = _svc(r["doc"])
    related = [(x["f"], x["t"]) for x in db.aql.execute(f"""
        FOR r IN `{p}_Relations` FILTER r.type == 'RELATED_TO'
          RETURN {{f: DOCUMENT(r._from).entity_name, t: DOCUMENT(r._to).entity_name}}""")
        if x.get("f") and x.get("t")]

    docs = sorted({r["doc"] for r in rows})
    G = nx.Graph()
    for d in docs:
        G.add_node(d, kind="doc")
    for r in rows:
        G.add_node(r["e"], kind="ent")
        G.add_edge(r["e"], r["doc"], kind="mention")
    for a, b in related:
        if a in G and b in G:
            G.add_edge(a, b, kind="related")

    pos = nx.spring_layout(G, seed=5, k=0.55, iterations=120)
    is_doc = {n: G.nodes[n].get("kind") == "doc" for n in G.nodes}
    deg = dict(G.degree())
    bridge = {n for n in G.nodes if not is_doc[n] and deg[n] > 1}  # entities shared across runbooks
    mention_e = [e for e in G.edges if G.edges[e]["kind"] == "mention"]
    related_e = [e for e in G.edges if G.edges[e]["kind"] == "related"]

    plt.figure(figsize=(13, 9))
    nx.draw_networkx_edges(G, pos, edgelist=mention_e, edge_color="#dcdcdc", width=0.8)
    nx.draw_networkx_edges(G, pos, edgelist=related_e, edge_color=AMBER, width=2.0, style="dashed")
    nx.draw_networkx_nodes(G, pos, nodelist=[n for n in G.nodes if not is_doc[n] and n not in bridge],
                           node_color=TEAL, node_size=90, alpha=0.8, edgecolors="white", linewidths=0.4)
    nx.draw_networkx_nodes(G, pos, nodelist=[n for n in bridge],
                           node_color="#d94e3a", node_size=180, edgecolors="white", linewidths=0.6)
    nx.draw_networkx_nodes(G, pos, nodelist=docs, node_color=NAVY, node_shape="s",
                           node_size=[700 + 120 * deg[d] for d in docs], edgecolors="white")
    nx.draw_networkx_labels(G, pos, {d: d.replace(".md", "") for d in docs}, font_size=7,
                            font_color="white", font_weight="bold")
    nx.draw_networkx_labels(G, pos, {n: n[:18] for n in bridge}, font_size=6, font_color="#7a2418")
    plt.title(f"AutoGraph knowledge graph — {len([n for n in G.nodes if not is_doc[n]])} entities extracted "
              f"from {len(docs)} runbooks (navy = runbook hub, red = entity shared across runbooks, "
              f"amber dashed = RELATED_TO)", fontsize=10)
    plt.axis("off")
    plt.tight_layout()
    plt.savefig("assets/knowledge-graph.png", dpi=130, bbox_inches="tight")
    plt.close()
    print(f"wrote assets/knowledge-graph.png ({len(G.nodes)} nodes: {len(docs)} runbooks + "
          f"{len([n for n in G.nodes if not is_doc[n]])} entities, {len(bridge)} shared)")


def fig_results_from_rows(rows):
    """The results showcase: one bar per alert (multimodel-query latency), colored by whether it
    grounded on the correct runbook, titled with the grounded/corroborated tally. Precomputed rows
    so the notebook can evaluate once and reuse them here. -> assets/results.png"""
    labels = [f"{r['alert']}  ·  {r['service']}" for r in rows]
    query_ms = [r["query_ms"] for r in rows]
    bar_colors = [GREEN if r["grounded"] else "#d94e3a" for r in rows]
    grounded = sum(r["grounded"] for r in rows)
    corroborated_count = sum(r["corroborated"] for r in rows)
    total = len(rows)
    positions = list(range(total))

    plt.figure(figsize=(10, 5.5))
    plt.barh(positions, query_ms, color=bar_colors, edgecolor="white")
    plt.yticks(positions, labels, fontsize=8)
    plt.gca().invert_yaxis()
    plt.xlabel("multimodel query — structured payload, one AQL round trip (ms)")
    for position, ms in zip(positions, query_ms):
        plt.text(ms + max(query_ms) * 0.01, position, f"{ms:.0f} ms", va="center", fontsize=7, color="#555")
    plt.title(f"{grounded}/{total} grounded on the correct runbook   ·   "
              f"{corroborated_count}/{total} corroborated   ·   "
              f"vector + graph + key-value in one round trip", fontsize=11)
    plt.tight_layout()
    plt.savefig("assets/results.png", dpi=130, bbox_inches="tight")
    plt.close()
    print(f"wrote assets/results.png ({grounded}/{total} grounded, {corroborated_count}/{total} corroborated)")


def fig_results(alerts_path="data/alerts.json"):
    """Standalone: evaluate every alert against the live deployment, then render the figure."""
    from resolver import evaluate
    fig_results_from_rows(evaluate(json.load(open(alerts_path))))


if __name__ == "__main__":
    fig_architecture()
    fig_affected_subgraph()
    fig_kg_sample()
    fig_results()
