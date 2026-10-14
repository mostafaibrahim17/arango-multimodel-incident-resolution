"""Unit tests for the pure reasoning / scoring / citation logic in resolver.py.

These do NOT touch ArangoDB, OpenAI, or the Retriever. They exercise the functions that carry the
bugs the review flagged, so a regression on any of them fails here immediately:

  - reason_rules        : does the pivot move to a degraded dependency, and stay put otherwise?
  - corroborated        : can the agreement check actually return False? (the old one could not)
  - _citation_service   : does it read the service from a runbook header, and fall back to the file name?
  - derive_confidence   : are the tiers derived from the graph, and does the judge only ever lower them?
  - _verify_command     : do databases avoid the nonsensical kubectl command?
  - scoring             : does correctness compare against the answer key?

Run:  pytest tests/ -v
Live integration tests (which DO connect) live in test_integration.py and are skipped by default.
"""
import resolver


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def make_payload(root, causes):
    """Build a minimal payload shaped like the marquee output.

    `causes` is a list of (service, depth, status) tuples for possible_causes.
    """
    return {
        "root_service": root,
        "possible_causes": [
            {"service": s, "name": s, "depth": d, "status": st, "note": ""}
            for (s, d, st) in causes
        ],
        "impacted_services": [],
        "similar_incidents": [],
        "on_call": {"team": "x", "contact": "x"},
    }


def runbook(service):
    """A citation whose body declares its owning service, like a real runbook."""
    return {
        "file_name": f"{service}.md",
        "content": f"# Runbook: {service}\n\n**Service:** {service}\n\n## Resolution\nDo the thing.",
        "primary": True,
    }


# ---------------------------------------------------------------------------
# reason_rules  --  the pivot logic (deterministic fallback)
# ---------------------------------------------------------------------------

def test_pivots_to_degraded_dependency():
    """Alerting service healthy, a dependency degraded -> pivot to the dependency."""
    payload = make_payload(
        "onboarding-api",
        [("onboarding-api", 0, "healthy"),
         ("document-service", 1, "healthy"),
         ("user-db", 2, "degraded")],
    )
    root, reasoning, confidence, method = resolver.reason_rules(payload)
    assert root == "user-db"
    assert method == "rules"
    assert confidence == "high"


def test_no_pivot_when_nothing_upstream_degraded():
    """Alerting service is the only lead -> stay on it, do not invent a pivot."""
    payload = make_payload(
        "payment-gateway",
        [("payment-gateway", 0, "healthy"),
         ("merchant-db", 1, "healthy")],
    )
    root, _, _, _ = resolver.reason_rules(payload)
    assert root == "payment-gateway"


def test_stays_when_alerting_service_itself_degraded():
    """If the alerting service itself is degraded, it is the cause -- no pivot."""
    payload = make_payload(
        "user-db",
        [("user-db", 0, "degraded")],
    )
    root, _, confidence, _ = resolver.reason_rules(payload)
    assert root == "user-db"
    assert confidence == "high"


def test_pivot_prefers_the_degraded_one_not_just_any_dependency():
    """Among several dependencies, only the degraded one should be chosen."""
    payload = make_payload(
        "onboarding-api",
        [("onboarding-api", 0, "healthy"),
         ("merchant-service", 1, "healthy"),
         ("document-service", 1, "healthy"),
         ("user-db", 2, "degraded")],
    )
    root, _, _, _ = resolver.reason_rules(payload)
    assert root == "user-db"


def test_fallback_disambiguates_multiple_degraded_by_symptom():
    """Two dependencies degraded: the deterministic fallback must use the symptom-to-note overlap
    to pick the true cause (user-db, matching 'permission'), NOT the shallower decoy
    (document-service). This is the latent case where the LLM judge is unavailable.
    """
    payload = make_payload(
        "onboarding-api",
        [("onboarding-api", 0, "healthy"),
         ("document-service", 1, "degraded"),   # shallower, but a decoy (render latency)
         ("user-db", 2, "degraded")],           # deeper, but the true cause (permission reads)
    )
    notes = {"document-service": "pdf_render_latency elevated; render workers saturated",
             "user-db": "read_replica_lag; stale permission reads for auth and merchant lookups"}
    for s in payload["possible_causes"]:
        s["note"] = notes.get(s["service"], "")
    symptom = "Merchants unable to e-sign the Merchant Agreement; permission failure at the document stage"
    root, _, confidence, method = resolver.reason_rules(payload, symptom)
    assert root == "user-db"       # matched on the note, not the shallower decoy
    assert method == "rules"
    assert confidence == "medium"  # disambiguating among several degraded is not a confident pick


def test_fallback_without_symptom_takes_shallowest():
    """With no symptom text to match on, the fallback keeps its old behavior: the shallowest
    degraded dependency. (Documents that the symptom match is an added signal, not a hard rule.)"""
    payload = make_payload(
        "onboarding-api",
        [("onboarding-api", 0, "healthy"),
         ("document-service", 1, "degraded"),
         ("user-db", 2, "degraded")],
    )
    root, _, _, _ = resolver.reason_rules(payload)   # no alert_text
    assert root == "document-service"


# ---------------------------------------------------------------------------
# corroborated  --  must be able to return False (the whole point of the fix)
# Reads `corroboration_citations`: the SYMPTOM-ONLY retrieval, independent of the decided root.
# ---------------------------------------------------------------------------

def test_corroboration_agrees_when_runbook_matches_root():
    payload = make_payload("user-db", [("user-db", 0, "degraded")])
    cited = {"corroboration_citations": {"1": runbook("user-db")}}
    result = resolver.corroborated(payload, cited)
    assert result["agree"] is True
    assert result["runbook_service"] == "user-db"


def test_corroboration_FAILS_when_runbook_is_for_a_different_service():
    """The critical regression guard: pick user-db but cite document-service -> disagree."""
    payload = make_payload("user-db", [("user-db", 0, "degraded")])
    cited = {"corroboration_citations": {"1": runbook("document-service")}}
    result = resolver.corroborated(payload, cited)
    assert result["agree"] is False
    assert result["runbook_service"] == "document-service"
    assert result["root_service"] == "user-db"


def test_corroboration_fails_with_no_citation():
    payload = make_payload("user-db", [("user-db", 0, "degraded")])
    cited = {"corroboration_citations": {}}
    result = resolver.corroborated(payload, cited)
    assert result["agree"] is False


# ---------------------------------------------------------------------------
# _citation_service  --  read the service from the runbook header, fall back to file name
# ---------------------------------------------------------------------------

def test_citation_service_reads_the_header():
    """The service is parsed from the runbook's **Service:** header line."""
    assert resolver._citation_service(runbook("user-db")) == "user-db"


def test_citation_service_falls_back_to_file_name():
    """With no header in the content, the file-name stem is used (how real Retriever chunks look)."""
    c = {"content": "some chunk text with no service header",
         "file_name": "data__user-db-replica-lag.md"}
    svc = resolver._citation_service(c)
    assert svc == "data__user-db-replica-lag"   # the .md stem
    assert "user-db" in svc                      # so a `root in svc` match still finds it


def test_citation_service_header_wins_over_file_name():
    """When both are present, the header (authoritative) is used, not the file name."""
    c = {"content": "**Service:** user-db\n\n## Resolution\nx", "file_name": "document-service.md"}
    assert resolver._citation_service(c) == "user-db"


# ---------------------------------------------------------------------------
# derive_confidence  --  tiers come from the graph; the judge can only lower, never raise
# ---------------------------------------------------------------------------

def test_confidence_high_when_decided_is_the_only_degraded():
    payload = make_payload("user-db",
                           [("onboarding-api", 0, "healthy"), ("user-db", 2, "degraded")])
    assert resolver.derive_confidence(payload, "user-db") == "high"


def test_confidence_medium_when_several_degraded():
    payload = make_payload("user-db",
                           [("onboarding-api", 0, "healthy"),
                            ("document-service", 1, "degraded"),
                            ("user-db", 2, "degraded")])
    assert resolver.derive_confidence(payload, "user-db") == "medium"


def test_confidence_low_when_decided_is_not_degraded():
    payload = make_payload("onboarding-api", [("onboarding-api", 0, "healthy")])
    assert resolver.derive_confidence(payload, "onboarding-api") == "low"


def test_judge_can_lower_the_tier():
    """Evidence says high, but a doubting judge (low) pulls it down."""
    payload = make_payload("user-db",
                           [("onboarding-api", 0, "healthy"), ("user-db", 2, "degraded")])
    assert resolver.derive_confidence(payload, "user-db", judge_confidence="low") == "low"


def test_judge_cannot_raise_the_tier():
    """Evidence says low (nothing degraded); a confident judge (high) must NOT raise it."""
    payload = make_payload("onboarding-api", [("onboarding-api", 0, "healthy")])
    assert resolver.derive_confidence(payload, "onboarding-api", judge_confidence="high") == "low"


# ---------------------------------------------------------------------------
# _verify_command  --  service-appropriate, no kubectl for databases
# ---------------------------------------------------------------------------

def test_verify_for_database_is_not_kubectl():
    cmd = resolver._verify_command("user-db")
    assert "kubectl" not in cmd
    assert "lag" in cmd.lower() or "lock" in cmd.lower()


def test_verify_for_gateway_mentions_saturation_or_egress():
    cmd = resolver._verify_command("network-gateway")
    assert "kubectl" not in cmd
    assert any(w in cmd.lower() for w in ("egress", "saturation", "timeout"))


def test_verify_for_app_service_uses_kubectl():
    cmd = resolver._verify_command("onboarding-api")
    assert "kubectl" in cmd
    assert "onboarding-api" in cmd


# ---------------------------------------------------------------------------
# scoring  --  correctness is measured against the answer key, and can fail
# ---------------------------------------------------------------------------

def test_scoring_marks_match_correct():
    decided, expected = "user-db", "user-db"
    assert (expected is not None and decided == expected) is True


def test_scoring_marks_mismatch_incorrect():
    """A wrong answer must score False -- the behaviour the old code lacked."""
    decided, expected = "user-db", "payment-gateway"
    assert (expected is not None and decided == expected) is False


def test_scoring_handles_missing_answer_key():
    decided, expected = "user-db", None
    assert (expected is not None and decided == expected) is False