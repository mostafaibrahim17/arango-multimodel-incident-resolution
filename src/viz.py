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


def _wrap(s, n=58):
    import textwrap
    return "\n".join(textwrap.wrap(s, n)) if s else ""


STAGE_COLORS = {
    "VECTOR": TEAL, "GRAPH": NAVY, "KEY-VALUE (health)": AMBER, "PIVOT": "#d94e3a",
    "PRECEDENT (vector #2)": TEAL, "ELIMINATE": "#d94e3a", "RETRIEVER": GREEN, "VERDICT": "#2e7d32",
}


def fig_reasoning_chain(reasoned):
    """The agent's reasoning, stage by stage: VECTOR -> GRAPH -> health -> PIVOT -> precedent ->
    eliminate -> GraphRAG citation -> verdict. Makes the multi-hop traversal tangible. -> assets/reasoning-chain.png"""
    chain = reasoned["reasoning_chain"]
    n = len(chain)
    fig, ax = plt.subplots(figsize=(12, 0.92 * n + 1.1))
    ax.set_xlim(0, 12)
    ax.set_ylim(0, n)
    ax.axis("off")
    for i, step in enumerate(chain):
        y = n - i - 1
        color = STAGE_COLORS.get(step["stage"], "#555")
        _box(ax, 0.2, y + 0.14, 2.9, 0.72, step["stage"], color, 8.5)
        ax.text(3.3, y + 0.5, _wrap(step["detail"], 86), va="center", ha="left",
                fontsize=8.3, color="#222")
        if i < n - 1:
            ax.annotate("", xy=(1.65, y + 0.14), xytext=(1.65, y - 0.14),
                        arrowprops=dict(arrowstyle="<|-", color="#bbb", lw=1.5))
    sym = reasoned["symptom_service"]
    root = reasoned["root_cause"]["service"]
    title = (f"How the agent reasons — alert on {sym}  →  real root cause: {root}"
             if reasoned["pivoted"] else f"How the agent reasons — alert on {sym}")
    ax.set_title(title, fontsize=11, pad=10)
    plt.tight_layout()
    plt.savefig("assets/reasoning-chain.png", dpi=130, bbox_inches="tight")
    plt.close()
    print("wrote assets/reasoning-chain.png")


def fig_ablation(reasoned):
    """Same alert, three retrieval strategies side by side, with which gets it right.
    The 10-second proof of the capability gap. -> assets/ablation.png"""
    abl = reasoned["ablation"]
    fig, ax = plt.subplots(figsize=(12.5, 3.4))
    ax.axis("off")
    cols = ["Configuration", "Root cause", "Recommended action", "Pages", "Verdict"]

    def _short(text, n=86):
        return text if len(text) <= n else text[:n].rstrip(" .,;") + "..."

    cell_text = [[a["config"], a["root"], _wrap(_short(a["action"]), 40), a["paged"] or "-", a["verdict"]]
                 for a in abl]
    table = ax.table(cellText=cell_text, colLabels=cols, loc="center", cellLoc="left",
                     colWidths=[0.16, 0.12, 0.40, 0.16, 0.18])
    table.auto_set_font_size(False)
    table.set_fontsize(8.5)
    table.scale(1, 3.0)
    for c in range(len(cols)):
        cell = table[0, c]
        cell.set_facecolor(NAVY)
        cell.set_text_props(color="white", fontweight="bold")
    for r, a in enumerate(abl, start=1):
        v = a["verdict"]
        fc = "#f4c7c3" if "WRONG" in v else "#fde9c8" if "no fix" in v else "#cdebcd"
        table[r, 4].set_facecolor(fc)
    ax.set_title("Same alert, three retrieval strategies — only the full multimodel query is right and cited",
                 fontsize=11, pad=14)
    plt.tight_layout()
    plt.savefig("assets/ablation.png", dpi=130, bbox_inches="tight")
    plt.close()
    print("wrote assets/ablation.png")


def fig_polyglot():
    """What the single AQL query replaces: a sequential polyglot 'Frankenstack' of separate
    vector + graph + key-value systems plus glue. -> assets/polyglot-vs-aql.png"""
    fig, ax = plt.subplots(figsize=(12.5, 5.6))
    ax.set_xlim(0, 12.5)
    ax.set_ylim(0, 5.6)
    ax.axis("off")
    # Left: the Frankenstack
    _box(ax, 0.2, 4.9, 5.9, 0.55, "Frankenstack — separate systems, sequential calls", "#7a2418", 10)
    steps = [
        "1.  vector store (e.g. Pinecone): similar incidents",
        "2.  graph DB (e.g. Neo4j): blast-radius traversal",
        "3.  key-value (e.g. Redis): per-service health signal",
        "4.  key-value (e.g. Redis): on-call owner",
        "5.  app code: join results, handle 4 partial failures",
    ]
    for i, s in enumerate(steps):
        _box(ax, 0.2, 4.05 - i * 0.78, 5.9, 0.6, s, TEAL if i < 4 else "#666", 8.5, "white")
    ax.text(3.15, 0.15, "4 systems · 4 auth contexts · 4 round trips · ~50 lines of glue",
            ha="center", fontsize=8.5, style="italic", color="#7a2418")
    # Right: one AQL query
    _box(ax, 6.4, 4.9, 5.9, 0.55, "Arango — one store, one AQL round trip", "#1f5f2e", 10)
    aql = ("LET similar  = (FOR i IN incidents       // vector\n"
           "    SORT APPROX_NEAR_COSINE(i.embedding,@vec) DESC LIMIT 3 RETURN i)\n"
           "LET affected = (FOR v IN 0..3 OUTBOUND    // graph\n"
           "    @leaf GRAPH 'service_topology' RETURN v)\n"
           "LET degraded = (FOR a IN affected         // key-value health\n"
           "    FILTER DOCUMENT('service_signals',a).status=='degraded' RETURN a)\n"
           "LET team     = DOCUMENT('teams',          // key-value owner\n"
           "    DOCUMENT('services', root).team)\n"
           "RETURN { similar, affected, degraded, team }")
    ax.add_patch(FancyBboxPatch((6.4, 0.55), 5.9, 4.05, boxstyle="round,pad=0.02,rounding_size=0.04",
                                fc="#0f1f17", ec="none"))
    ax.text(6.6, 4.45, aql, va="top", ha="left", fontsize=7.3, family="monospace", color="#cfe8d4")
    ax.text(9.35, 0.15, "1 system · 1 auth context · 1 round trip · no glue",
            ha="center", fontsize=8.5, style="italic", color="#1f5f2e")
    plt.tight_layout()
    plt.savefig("assets/polyglot-vs-aql.png", dpi=130, bbox_inches="tight")
    plt.close()
    print("wrote assets/polyglot-vs-aql.png")


def fig_cascade_subgraph(reasoned):
    """The dependency subgraph for the hero alert, highlighting the cascade: the alerting leaf
    (amber) and the degraded upstream root cause (red), with the path between them. -> assets/affected-subgraph.png"""
    affected = {a["service"]: a for a in reasoned["structure"]["affected_services"]}
    topo = json.load(open("data/topology.json"))
    names = {k: v["name"] for k, v in topo["services"].items()}
    sym = reasoned["symptom_service"]
    root = reasoned["root_cause"]["service"]
    G = nx.DiGraph()
    for a in affected:
        G.add_node(a)
    for fr, to in topo["depends_on"]:
        if fr in affected and to in affected:
            G.add_edge(fr, to)
    pos = nx.spring_layout(G, seed=7, k=1.3)
    depth = {k: v["depth"] for k, v in affected.items()}
    colors = []
    for node in G.nodes:
        if node == sym:
            colors.append(AMBER)
        elif node == root:
            colors.append("#d94e3a")
        else:
            colors.append([TEAL, "#69a6a6", "#9ccb6a", "#c3dea0"][min(depth[node], 3)])
    # highlight the shortest path leaf -> root if present
    path_edges = []
    try:
        sp = nx.shortest_path(G, sym, root)
        path_edges = list(zip(sp, sp[1:]))
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        pass
    plt.figure(figsize=(9.5, 6.2))
    nx.draw_networkx_edges(G, pos, edge_color="#ccc", arrows=True, arrowsize=13, width=1.2)
    if path_edges:
        nx.draw_networkx_edges(G, pos, edgelist=path_edges, edge_color="#d94e3a",
                               arrows=True, arrowsize=18, width=2.6)
    nx.draw_networkx_nodes(G, pos, node_color=colors, node_size=2300, edgecolors="white")
    nx.draw_networkx_labels(G, pos, {n: names.get(n, n) for n in G.nodes}, font_size=8)
    title = (f"Alert fired on {names.get(sym, sym)} (amber); real root cause {names.get(root, root)} "
             f"(red), {reasoned['root_cause']['depth']} hops upstream"
             if reasoned["pivoted"] else f"Affected-service subgraph for {names.get(sym, sym)}")
    plt.title(title, fontsize=10.5)
    plt.axis("off")
    plt.tight_layout()
    plt.savefig("assets/affected-subgraph.png", dpi=130, bbox_inches="tight")
    plt.close()
    print("wrote assets/affected-subgraph.png")


def cascade_figures(alert_path="data/alert.hero.json", reasoned=None):
    """Render all cascade figures from one reason() pass (compute once, reuse)."""
    if reasoned is None:
        from resolver import reason
        reasoned = reason(json.load(open(alert_path)))
    fig_reasoning_chain(reasoned)
    fig_ablation(reasoned)
    fig_cascade_subgraph(reasoned)
    fig_polyglot()
    return reasoned


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
