"""Resolve one alert in a single AQL round trip.

Vector search finds similar past incidents, a graph traversal expands both the cause-side
(dependencies) and the impact-side (dependents) subgraphs, and a key-value lookup returns the
on-call team. Document, vector, graph, and key-value access all happen in one query.
"""
import json
import sys
import time

import requests
from openai import OpenAI

from graphrag import retriever_url, runbook_for_service, token
from ingest import EMBED_MODEL, connect

# One query, three multimodel moves: APPROX_NEAR_COSINE (vector), OUTBOUND/INBOUND (graph),
# DOCUMENT (key-value).
#
# The two traversal directions answer two different questions over `A depends_on B`:
#   OUTBOUND from `root` -> the services `root` DEPENDS ON  -> where a cause may live.
#   INBOUND  to   `root` -> the services that DEPEND ON it  -> who is impacted (blast radius).
# Both are returned: `possible_causes` (OUTBOUND) feeds root-cause reasoning, and
# `impacted_services` (INBOUND) is the blast radius.
MARQUEE = """
LET similar = (
  FOR i IN incidents
    LET sim = APPROX_NEAR_COSINE(i.embedding, @vec)
    SORT sim DESC
    LIMIT 3
    RETURN {
      number: i._key,
      short_description: i.short_description,
      resolution: i.resolution,
      service: i.service,
      similarity: sim
    }
)
LET root = @service != null ? @service : FIRST(similar).service
LET possible_causes = (
  FOR v, e, p IN 0..3 OUTBOUND CONCAT("services/", root) GRAPH "service_topology"
    LET health = DOCUMENT("service_health", CONCAT(@scenario, "__", v._key))
    COLLECT service = v._key, name = v.name, status = health.status, note = health.note
      AGGREGATE depth = MIN(LENGTH(p.edges))
    SORT depth
    RETURN {service, name, depth, status, note}
)
LET impacted_services = (
  FOR v, e, p IN 1..3 INBOUND CONCAT("services/", root) GRAPH "service_topology"
    LET health = DOCUMENT("service_health", CONCAT(@scenario, "__", v._key))
    COLLECT service = v._key, name = v.name, status = health.status, note = health.note
      AGGREGATE depth = MIN(LENGTH(p.edges))
    SORT depth
    RETURN {service, name, depth, status, note}
)
LET team = DOCUMENT("teams", DOCUMENT("services", root).team)
RETURN {
  root_service: root,
  similar_incidents: similar,
  possible_causes: possible_causes,
  impacted_services: impacted_services,
  on_call: {team: team.name, contact: team.oncall}
}
"""

REASONING_MODEL = "gpt-5-mini"

# Each alert carries a `scenario` naming its incident; health is stored per scenario
# (keyed `<scenario>__<service>`). If an alert has no scenario, fall back to the all-clear snapshot
# (every service healthy) rather than the hero's udb-lag: inheriting the hero's degraded services
# would make an unlabeled alert pivot to user-db for no reason. With all-clear the resolver sees no
# degraded dependency, stays on the alerting service, and reports low confidence -- the honest
# default when the scenario is unknown.
DEFAULT_SCENARIO = "all-clear"

REASONING_PROMPT = """You are a senior SRE deciding the true root cause of a production incident.

You receive the evidence one multimodel query already gathered: the alert, the top similar past
incidents (vector search), and the dependency chain of the alerting service (the services it
DEPENDS ON) with each service's CURRENT health status (graph traversal + health join). A cause
lives among the services the alerting service depends on, not among the services that depend on it.

Decide which single service is the true root cause:
- The alerting service is the default hypothesis.
- Pivot to a dependency ONLY if that dependency is degraded AND its degradation plausibly
  explains the alert's symptom. Depth matters: a degraded direct dependency (depth 1) usually
  explains the symptom better than one further away.
- If several services are degraded, pick the one whose health note best matches the symptom.
- Never invent a service: root_service MUST be the exact `service` key (e.g. "document-service",
  not the display name "Document Service") of one of the entries in possible_causes.

Return reasoning as 1-2 short sentences citing the evidence (service, depth, status/note).
confidence: high = the evidence cleanly explains the symptom; medium = plausible but not
conclusive; low = evidence is thin or conflicting."""


def _degraded_candidates(causes):
    """Degraded dependencies (depth > 0), ordered closest-first."""
    degraded = [s for s in causes if s["depth"] > 0 and s.get("status") == "degraded"]
    degraded.sort(key=lambda s: s["depth"])
    return degraded


_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "in", "on", "at", "for", "with", "is", "are",
    "after", "its", "it", "this", "that", "as", "by", "from", "into", "step", "ui", "error",
    "service", "high", "above", "below", "near", "not",
}


def _tokens(text):
    """Lowercased content words of `text`, for a light symptom/note overlap check."""
    import re
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 2 and w not in _STOPWORDS}


def _best_symptom_match(candidates, alert_text):
    """Among several degraded dependencies, the one whose health note best matches the symptom.

    The deterministic fallback cannot weigh intent the way the LLM judge does, so when more than one
    dependency is degraded it breaks the tie by word overlap between the symptom and each service's
    health note: a "permission error" symptom matches user-db's "stale permission reads", not
    document-service's "render latency". `candidates` is shallowest-first, so with no overlap at all
    the pick stays the shallowest (the previous behavior).
    """
    symptom = _tokens(alert_text)
    if not symptom:
        return candidates[0]
    best, best_score = candidates[0], -1
    for c in candidates:
        score = len(symptom & _tokens(c.get("note")))
        if score > best_score:
            best, best_score = c, score
    return best if best_score > 0 else candidates[0]


def reason_rules(payload, alert_text=None):
    """Deterministic fallback: pivot to a degraded upstream dependency, if any.

    With one degraded dependency the pick is that service. With several, the tie is broken by which
    service's health note best matches the symptom (see _best_symptom_match), so the fallback does
    not blindly take the shallowest degraded service, which may be a decoy. Used only when the LLM
    judge is unavailable.
    """
    alerting_service = payload["root_service"]
    causes = payload["possible_causes"]

    alerting_health = next(
        (s for s in causes if s["service"] == alerting_service), {}
    ).get("status", "unknown")

    candidates = _degraded_candidates(causes)

    if alerting_health != "degraded" and candidates:
        pick = candidates[0] if len(candidates) == 1 else _best_symptom_match(candidates, alert_text)
        true_root = pick["service"]
        # One clear degraded dependency is a confident pick; disambiguating among several is not.
        confidence = "high" if len(candidates) == 1 else "medium"
        reasoning = (
            f"{alerting_service} is reporting the symptom, but {true_root} "
            f"(depth {pick['depth']}) is degraded. Pivoting root cause."
        )
    else:
        true_root = alerting_service
        confidence = "high" if alerting_health == "degraded" else "medium"
        reasoning = (
            f"{alerting_service} is the alerting service "
            f"(status: {alerting_health}); no degraded upstream dependency to pivot to."
        )
    return true_root, reasoning, confidence, "rules"


def derive_confidence(payload, decided_root, judge_confidence=None):
    """Derive the finding's confidence from the graph evidence, not from the model's self-report.

    The confidence tier is grounded in what the health-annotated graph actually shows:
      - high   : the decided root is degraded AND it is the only degraded service in the chain.
                 The graph points decisively at one culprit.
      - medium : the decided root is degraded, but other services are degraded too, so the pick is
                 plausible rather than decisive.
      - low    : the decided root is not degraded, so no health signal supports it.

    The model's own confidence (judge_confidence) is used only as a downward cap: a judge that is
    itself unsure can lower the tier but never raise it above what the evidence supports.
    """
    status_by = {s["service"]: s.get("status") for s in payload["possible_causes"]}
    degraded = [s for s in payload["possible_causes"] if s.get("status") == "degraded"]
    decided_is_degraded = status_by.get(decided_root) == "degraded"

    if not decided_is_degraded:
        tier = "low"
    elif len(degraded) == 1:
        tier = "high"
    else:
        tier = "medium"

    order = {"low": 0, "medium": 1, "high": 2}
    if judge_confidence in order and order[judge_confidence] < order[tier]:
        tier = judge_confidence
    return tier


def confidence_basis(payload, decided_root, judge_confidence=None):
    """A one-line, human-readable explanation of the reported confidence tier: what the graph
    evidence shows, and how the judge's self-report affected it. Mirrors derive_confidence so the
    output explains itself (why `confidence` can differ from `judge_confidence`) instead of
    requiring the capping rule to be known.
    """
    degraded = [s for s in payload["possible_causes"] if s.get("status") == "degraded"]
    status_by = {s["service"]: s.get("status") for s in payload["possible_causes"]}
    decided_is_degraded = status_by.get(decided_root) == "degraded"

    if not decided_is_degraded:
        evidence_tier = "low"
        evidence = (f"decided root ({decided_root}) is not degraded, "
                    f"so the graph gives no supporting signal")
    elif len(degraded) == 1:
        evidence_tier = "high"
        evidence = f"decided root ({decided_root}) is the only degraded service in the chain"
    else:
        evidence_tier = "medium"
        evidence = (f"decided root ({decided_root}) is degraded, but {len(degraded)} services in "
                    f"the chain are degraded, so the pick is plausible rather than decisive")

    order = {"low": 0, "medium": 1, "high": 2}
    final = evidence_tier
    if judge_confidence not in order:
        judge_clause = ""
    elif order[judge_confidence] < order[evidence_tier]:
        final = judge_confidence
        judge_clause = f"; judge self-reported {judge_confidence}, lowering the tier"
    elif order[judge_confidence] > order[evidence_tier]:
        judge_clause = (f"; judge self-reported {judge_confidence} but confidence is "
                        f"capped to the evidence")
    else:
        judge_clause = f"; judge agreed ({judge_confidence})"
    return f"{final} — {evidence}{judge_clause}"


def reason(payload, alert_text=None, db=None):
    """The reasoning layer.

    An LLM judges the evidence the multimodel query gathered and decides the true root cause,
    pivoting away from the alerting service when a degraded dependency better explains the symptom.
    Falls back to the deterministic rule if the API call fails or returns a service outside the
    dependency set. The reported `confidence` is then derived from the graph evidence (see
    derive_confidence); the model's own self-reported confidence is kept separately as
    `judge_confidence` for transparency.
    """
    causes = payload["possible_causes"]
    known_services = {s["service"] for s in causes}

    try:
        evidence = {
            "alert": {
                "service": payload["root_service"],
                "symptom": alert_text or (
                    payload["similar_incidents"][0].get("short_description", "")
                    if payload.get("similar_incidents") else ""
                ),
            },
            "similar_incidents": [
                {k: inc[k] for k in ("number", "short_description", "service", "similarity")}
                for inc in payload.get("similar_incidents", [])
            ],
            "possible_causes": causes,
        }
        response = OpenAI().chat.completions.create(
            model=REASONING_MODEL,
            reasoning_effort="minimal",  # the evidence packet is small; minimal keeps the judge ~2s
            messages=[
                {"role": "system", "content": REASONING_PROMPT},
                {"role": "user", "content": json.dumps(evidence)},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "root_cause_decision",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "root_service": {"type": "string"},
                            "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                            "reasoning": {"type": "string"},
                        },
                        "required": ["root_service", "confidence", "reasoning"],
                        "additionalProperties": False,
                    },
                },
            },
        )
        decision = json.loads(response.choices[0].message.content)
        true_root = decision["root_service"]
        if true_root not in known_services:
            # Tolerate the display name ("Document Service") by mapping it back to the key.
            by_name = {s["name"].lower(): s["service"] for s in causes if s.get("name")}
            true_root = by_name.get(true_root.lower())
            if true_root is None:
                raise ValueError(f"LLM proposed unknown service {decision['root_service']!r}")
        reasoning, judge_confidence, method = decision["reasoning"], decision["confidence"], "llm-judge"
    except Exception as exc:  # API failure or invalid decision -> deterministic fallback
        print(f"[reason] LLM judge unavailable ({exc}); using rules fallback", file=sys.stderr)
        true_root, reasoning, judge_confidence, method = reason_rules(payload, alert_text)

    # The traversal computed the on-call owner AND the blast radius for the ALERTING service. If
    # reasoning pivoted to a different root, both must be re-pointed at the decided root: the owner
    # to that service's team, and the blast radius (impacted_services) to the services that depend
    # on the decided root -- otherwise the reported blast radius is still the alerting service's,
    # not the true cause's.
    if db is not None and true_root != payload["root_service"]:
        svc = db.collection("services").get(true_root)
        team = db.collection("teams").get(svc["team"]) if svc and svc.get("team") else None
        if team:
            payload["on_call"] = {"team": team["name"], "contact": team["oncall"]}
        payload["impacted_services"] = _impacted_for(
            true_root, payload.get("scenario") or DEFAULT_SCENARIO, db)

    payload["root_service"] = true_root
    payload["reasoning"] = reasoning
    payload["confidence"] = derive_confidence(payload, true_root, judge_confidence)
    payload["judge_confidence"] = judge_confidence
    payload["confidence_basis"] = confidence_basis(payload, true_root, judge_confidence)
    payload["method"] = method
    payload["verify"] = _verify_command(true_root, db)
    return payload


_IMPACTED_AQL = """
FOR v, e, p IN 1..3 INBOUND CONCAT("services/", @root) GRAPH "service_topology"
  LET health = DOCUMENT("service_health", CONCAT(@scenario, "__", v._key))
  COLLECT service = v._key, name = v.name, status = health.status, note = health.note
    AGGREGATE depth = MIN(LENGTH(p.edges))
  SORT depth
  RETURN {service, name, depth, status, note}
"""


def _impacted_for(root, scenario, db):
    """Recompute the blast radius (INBOUND dependents) for a service, after a root-cause pivot.

    MARQUEE computed impacted_services for the ALERTING service. Once reasoning pivots to a deeper
    root (e.g. onboarding-api -> user-db), the blast radius must be the dependents of the DECIDED
    root, so this re-runs the same INBOUND traversal from there. Returns [] on any failure so a
    pivot never breaks resolution.
    """
    try:
        return list(db.aql.execute(_IMPACTED_AQL, bind_vars={"root": root, "scenario": scenario}))
    except Exception as exc:
        print(f"[reason] could not recompute blast radius for {root} ({exc})", file=sys.stderr)
        return []


def _verify_command(service, db=None):
    """Return a first verification step appropriate to the kind of service.

    Databases and gateways are not plain kubernetes workloads, so the check is chosen to match
    the service type rather than always suggesting a pod-level command.
    """
    db_like = service.endswith("-db") or service in {"user-db", "merchant-db"}
    if db_like:
        return (f"Check {service} replication lag and active locks "
                f"(e.g. replica_lag_seconds, slow-query log on the primary).")
    gateway_like = service.endswith("-gateway")
    if gateway_like:
        return (f"Check {service} egress/saturation metrics and upstream reachability "
                f"(error rate, connection pool, outbound timeouts).")
    return f"kubectl top pods -l app={service} && kubectl logs -l app={service} --tail=50"


def resolve_timed(alert, db=None):
    """resolve() plus timings: how long the alert text took to embed vs. the multimodel AQL itself.

    Returns (payload, embed_ms, query_ms). query_ms is the one AQL round trip (vector + graph +
    key-value) on its own; the embedding round trip to OpenAI is timed separately so the query's
    real latency is visible rather than hidden behind the embed call.
    """
    db = db or connect()
    embed_start = time.perf_counter()
    vec = OpenAI().embeddings.create(model=EMBED_MODEL, input=[alert["text"]]).data[0].embedding
    embed_ms = (time.perf_counter() - embed_start) * 1000

    scenario = alert.get("scenario") or DEFAULT_SCENARIO
    query_start = time.perf_counter()
    payload = db.aql.execute(MARQUEE, bind_vars={
        "vec": vec,
        "service": alert.get("service"),
        "scenario": scenario,
    }).next()
    query_ms = (time.perf_counter() - query_start) * 1000
    payload["scenario"] = scenario  # so a root-cause pivot can recompute the blast radius per-scenario
    payload = reason(payload, alert_text=alert.get("text"), db=db)
    return payload, embed_ms, query_ms


def resolve(alert):
    """Given an alert dict ({text, service?}), return the structured resolution payload."""
    payload, _embed_ms, _query_ms = resolve_timed(alert)
    return payload


def _runbook_resolution_snippet(content):
    """A short resolution-oriented snippet from a runbook body, for the degraded (Retriever-down)
    fallback text. Prefers the lines under the runbook's `## Resolution` heading; falls back to the
    first substantial line.
    """
    lines = (content or "").splitlines()
    out, capture = [], False
    for line in lines:
        stripped = line.strip()
        if stripped.lower().startswith("## resolution"):
            capture = True
            continue
        if capture:
            if stripped.startswith("## "):  # reached the next section
                break
            if stripped:
                out.append(stripped.lstrip("0123456789.-* ").strip())
            if len(out) >= 2:
                break
    if out:
        return " ".join(out)[:300]
    for line in lines:  # no Resolution section found; take the first substantial line
        clean = line.strip().lstrip("#*->0123456789. ").strip()
        if len(clean) > 30:
            return clean[:300]
    return ""


def _citation_service(citation):
    """The service a citation is about: parsed from its `**Service:** <svc>` header if present,
    otherwise inferred from the runbook file name (e.g. `data__user-db-replica-lag.md`)."""
    for line in (citation.get("content") or "").splitlines():
        low = line.lower()
        if "**service:**" in low:
            return low.split("**service:**", 1)[1].strip().strip("`").strip()
    name = (citation.get("file_name") or "").lower()
    return name[:-3] if name.endswith(".md") else name


def answer(payload, alert_text=None, jwt=None):
    """Cited next-step for the decided root service, grounded in the runbook knowledge graph.

    Two separate things come back, and they are kept separate on purpose:

      - `primary_runbook`: the authoritative runbook for the decided root service, read straight
        from the KG store (`runbook_for_service`, an ArangoDB query). It does not depend on the
        Retriever, so it survives a Retriever outage.
      - `citations`: the Retriever's own cited sources, returned under the Retriever's own numbering
        (`citation_mapping`). The answer text refers to these by number, so they are passed through
        unchanged. An earlier version inserted the pinned runbook at [1] and shifted every other
        citation down by one, which left the text's [1] pointing at the wrong source. Keeping the
        pinned runbook out of this list fixes that.

    The Retriever call is the only network dependency here, so it is best-effort: if auth, service
    discovery, or the query itself fails, the resolver still returns `primary_runbook` and sets
    `retriever: "unavailable"` instead of failing the whole resolution.
    """
    service = payload["root_service"]
    symptom = alert_text or (payload["similar_incidents"][0].get("short_description", "")
                             if payload.get("similar_incidents") else "")

    # Authoritative runbook for the decided root, from the KG store. Reliable: a database read, not
    # a Retriever call, so it survives a Retriever outage. Kept separate from the Retriever's
    # citations so the Retriever's numbering is never disturbed.
    primary_runbook = runbook_for_service(service)

    try:
        jwt = jwt or token()
        query = (f"{service} incident: {symptom} "
                 f"What does the {service} runbook recommend as the resolution and the first on-call step?")
        response = requests.post(
            f"{retriever_url(jwt=jwt)}/graphrag-query",
            headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
            json={"query": query, "query_type": 3,
                  "show_citations": True, "include_metadata": True, "use_cache": False},
            timeout=180,
        )
        response.raise_for_status()
        body = response.json()
        metadata = json.loads(body.get("metadata", "{}"))
        citations = metadata.get("citation_mapping", {}) or {}  # the Retriever's own numbering, kept as-is
        # Corroboration citations: a SEPARATE retrieval driven by the SYMPTOM ALONE, never naming
        # the decided service. The answer query above names `service`, so its citations are
        # steered toward the decision and cannot independently corroborate it. This second query
        # asks retrieval "given only what the alert reported, which runbook do you surface?" -- a
        # genuinely independent vote that can disagree with the graph's pick. See corroborated().
        corroboration_citations = _retrieve_for_symptom(symptom, jwt)
        return {"text": body.get("result", ""), "citations": citations,
                "corroboration_citations": corroboration_citations,
                "primary_runbook": primary_runbook, "retriever": "ok"}
    except Exception as exc:  # auth / discovery / query failure -> pinned runbook only
        print(f"[answer] Retriever unavailable ({exc}); returning pinned runbook only",
              file=sys.stderr)
        snippet = _runbook_resolution_snippet(primary_runbook["content"]) if primary_runbook else ""
        text = (f"Retriever unavailable; grounded on the {service} runbook. "
                f"Recommended resolution: {snippet}") if snippet else (
                f"Retriever unavailable; see the {service} runbook.")
        return {"text": text, "citations": {}, "corroboration_citations": {},
                "primary_runbook": primary_runbook, "retriever": "unavailable"}


def _retrieve_for_symptom(symptom, jwt):
    """Run a SYMPTOM-ONLY retrieval and return its citation mapping, for corroboration.

    This is the independent surface in the corroboration check. It deliberately does NOT mention
    the decided root service: it asks the Retriever only what the alert itself reported, so the
    runbook it surfaces is retrieval's own vote, not an echo of the graph's decision. Best-effort:
    on any failure it returns {} and corroboration reports no agreement rather than raising.
    """
    try:
        query = (f"Incident symptom: {symptom} "
                 f"Which service's runbook describes this failure and its resolution?")
        response = requests.post(
            f"{retriever_url(jwt=jwt)}/graphrag-query",
            headers={"Authorization": f"Bearer {jwt}", "Content-Type": "application/json"},
            json={"query": query, "query_type": 3,
                  "show_citations": True, "include_metadata": True, "use_cache": False},
            timeout=180,
        )
        response.raise_for_status()
        metadata = json.loads(response.json().get("metadata", "{}"))
        return metadata.get("citation_mapping", {}) or {}
    except Exception as exc:
        print(f"[answer] symptom-only corroboration retrieval unavailable ({exc})", file=sys.stderr)
        return {}


def corroborated(payload, cited):
    """Independent corroboration between the two surfaces.

    The structured pipeline chooses a root service from the graph and health. Corroboration asks a
    separate question: given ONLY the alert's symptom, does the Retriever's semantic search surface
    that same service's runbook? It reads `corroboration_citations` -- the result of a symptom-only
    retrieval that never names the decided service (see _retrieve_for_symptom) -- NOT the answer's
    own citations, which were steered by a query naming the root and so would agree by construction.

    Because the two surfaces are now driven by different inputs (graph+health vs. symptom text),
    this genuinely can disagree: when search alone can't make a multi-hop pivot (e.g. the hero,
    where the symptom reads like an onboarding/permission issue but the real cause is user-db two
    hops away), it surfaces a different runbook and corroboration returns False. That is the honest
    signal -- search and the graph really do disagree there -- not a failure of the check.
    """
    root = payload["root_service"].lower()
    citations = cited.get("corroboration_citations", {})
    services = [_citation_service(c) for c in citations.values()]
    match = next((s for s in services if s == root or (s and root in s)), None)
    agree = match is not None
    return {"agree": agree,
            "runbook_service": match or (services[0] if services else None),
            "root_service": payload["root_service"]}


def _first_sentence(text):
    """A short, plain-text snippet of the cited answer (drop markdown headings/bullets)."""
    for line in text.splitlines():
        clean = line.lstrip("#*-> ").strip()
        if len(clean) > 30:
            return clean[:160]
    return text.strip()[:160]


def evaluate(alerts, jwt=None):
    """Run every alert end to end and return one result row per alert.

    Accuracy is measured against a ground-truth answer key: each alert carries `expected_root`,
    the service confirmed as the true cause. `correct` compares the pipeline's decided root_service
    against that key. `runbook_found` reports whether a runbook was located for the decided root
    (a coverage flag, not an accuracy measure), and `corroborated` reports whether the Retriever's
    own retrieval independently surfaced that service's runbook. `confidence` is the derived tier and
    `judge_confidence` is the model's own self-report. Timings cover the multimodel query (one AQL
    round trip) and the cited Retriever answer.
    """
    try:
        jwt = jwt or token()
    except Exception as exc:  # no auth -> every answer() falls back to runbook-only
        print(f"[evaluate] auth unavailable ({exc}); answers will run runbook-only", file=sys.stderr)
        jwt = None
    db = connect()
    rows = []
    for alert in alerts:
        payload, embed_ms, query_ms = resolve_timed(alert, db=db)

        answer_start = time.perf_counter()
        cited = answer(payload, alert.get("text"), jwt=jwt)
        answer_seconds = time.perf_counter() - answer_start

        primary = cited.get("primary_runbook")
        top_similar = payload["similar_incidents"][0] if payload["similar_incidents"] else None
        expected = alert.get("expected_root")
        decided = payload["root_service"]
        rows.append({
            "alert": alert["_key"],
            "service": alert["service"],
            "severity": alert["severity"],
            "expected_root": expected,
            "decided_root": decided,
            "correct": (expected is not None and decided == expected),
            "confidence": payload["confidence"],
            "judge_confidence": payload.get("judge_confidence"),
            "confidence_basis": payload.get("confidence_basis"),
            "top_similar_incident": top_similar["number"] if top_similar else None,
            "top_similarity": round(top_similar["similarity"], 3) if top_similar else None,
            "impact_radius": len(payload["impacted_services"]),
            "on_call": payload["on_call"]["team"],
            "primary_runbook": primary["file_name"] if primary else None,
            "runbook_found": primary is not None,
            "corroborated": corroborated(payload, cited)["agree"],
            "retriever": cited.get("retriever"),
            "citations": len(cited.get("citations", {})),
            "query_ms": round(query_ms, 1),
            "embed_ms": round(embed_ms, 1),
            "answer_s": round(answer_seconds, 1),
            "next_step": _first_sentence(cited["text"]),
        })
    return rows


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "data/alert.sample.json"
    alert = json.load(open(path))
    payload = resolve(alert)
    cited = answer(payload, alert.get("text"))
    print(json.dumps({
        "structured": payload,
        "cited_answer": cited,
        "corroboration": corroborated(payload, cited),
    }, indent=2))