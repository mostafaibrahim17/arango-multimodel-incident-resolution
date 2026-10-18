"""Check Arango and OpenAI credentials before running the pipeline.

Run this first: it fails fast with a readable message instead of a stack trace halfway
through ingest. Exits non-zero if any check fails.
"""
import os
import sys

import openai
from arango import ArangoClient
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# Keep in sync with src/ingest.py and src/resolver.py.
EMBED_MODEL = "text-embedding-3-small"
REASONING_MODEL = "gpt-5-mini"


def missing(*names):
    """Return the env vars in names that are unset or empty."""
    return [n for n in names if not os.getenv(n)]


def check_arango():
    gaps = missing("ARANGO_HOST", "ARANGO_USER", "ARANGO_PASSWORD")
    if gaps:
        return False, f"missing {', '.join(gaps)} (set them in .env or your environment)"
    try:
        client = ArangoClient(hosts=os.environ["ARANGO_HOST"])
        # verify=True makes a real authenticated request; without it client.db() never contacts the server.
        db = client.db(
            "_system",
            username=os.environ["ARANGO_USER"],
            password=os.environ["ARANGO_PASSWORD"],
            verify=True,
        )
        return True, f"connected to {os.environ['ARANGO_HOST']} (ArangoDB {db.version()})"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def check_openai():
    if missing("OPENAI_API_KEY"):
        if not missing("LLM_PROVIDER_API_KEY"):
            return False, (
                "found LLM_PROVIDER_API_KEY but not OPENAI_API_KEY; the pipeline's OpenAI() "
                "client only reads OPENAI_API_KEY, so rename it in .env"
            )
        return False, "missing OPENAI_API_KEY (set it in .env or your environment)"
    client = OpenAI()
    try:
        # A one-token embedding exercises the exact call ingest makes first; cost is negligible.
        client.embeddings.create(model=EMBED_MODEL, input=["credential check"])
        client.models.retrieve(REASONING_MODEL)
        return True, f"API key valid; {EMBED_MODEL} and {REASONING_MODEL} available"
    except openai.AuthenticationError:
        return False, "OPENAI_API_KEY was rejected (401). Check the key at https://platform.openai.com/api-keys"
    except openai.NotFoundError as e:
        return False, f"API key valid but a required model is not available to this account: {e}"
    except openai.PermissionDeniedError as e:
        return False, f"API key lacks permission (403): {e}"
    except openai.RateLimitError as e:
        return False, f"rate limited or out of quota (429); check billing: {e}"
    except openai.APIConnectionError as e:
        return False, f"could not reach the OpenAI API: {e}"


def main():
    results = [("Arango", check_arango()), ("OpenAI", check_openai())]
    for name, (ok, msg) in results:
        print(f"[{'OK' if ok else 'FAIL'}] {name}: {msg}")
    if not all(ok for _, (ok, _) in results):
        sys.exit(1)
    print("All credentials look good.")


if __name__ == "__main__":
    main()
