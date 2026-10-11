import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

"""Shared test setup.

resolver.py imports ingest.py and graphrag.py at module load, and both read ARANGO_* /
OPENAI_API_KEY from the environment at import time. None of the pure-logic functions under test
actually connect to anything, but the import would fail without those vars present. So we set
harmless dummy values here, before any test module imports resolver. Real integration tests (which
DO connect) read the real values from a loaded .env instead and are skipped unless RUN_LIVE=1.
"""
import os

_DUMMY = {
    "ARANGO_HOST": "http://localhost:8529",
    "ARANGO_DB": "incident_demo",
    "ARANGO_USER": "root",
    "ARANGO_PASSWORD": "test",
    "OPENAI_API_KEY": "sk-test-not-used-in-unit-tests",
    "GRAPHRAG_DB": "incident_demo",
    "GRAPHRAG_PROJECT": "incident-runbook-autograph",
}
for key, value in _DUMMY.items():
    os.environ.setdefault(key, value)