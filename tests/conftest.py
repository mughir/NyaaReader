"""
Shared test setup.

Three jobs, each earned by a real incident during development:

1. Isolate all state under a session-local temp directory, set up BEFORE any
   test module imports backend.main / database (both call init_db() at
   IMPORT time, so DATABASE_URL / DATA_DIR must already be correct).

2. Scrub relay credentials from os.environ and neuter urllib.request.urlopen.
   backend/translator.py and backend/database.py both call load_dotenv() at
   IMPORT TIME, which RE-READS the developer's real .env and repopulates
   FALLBACK_API_KEY even if it was cleared before import — during
   development this made a test suite spend real relay credit while
   believing credentials were absent. Scrubbing must happen AFTER importing
   `main` (which pulls in translator), not only before.

3. Give every test its own uniquely-named novel. main.py's batch-job locks,
   caches and the translator singleton are process-global state; per-test
   isolation here comes from every test creating novels with a name/URL
   unique to that test rather than from resetting shared state between
   tests (which the app's own architecture doesn't expose a hook for).
"""
import os
import sys
import tempfile
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
for _p in (BACKEND, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_WORK = tempfile.mkdtemp(prefix="nyaa-tests-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_WORK, "novel_reader.db").replace("\\", "/")
os.environ["DATA_DIR"] = os.path.join(_WORK, "data")
for _k in ("FALLBACK_API_KEY", "GEMINI_API_KEY", "FALLBACK_2_API_KEY"):
    os.environ.pop(_k, None)

import main as app_module  # noqa: E402 — import must follow the env setup above

# Re-scrub AFTER import: translator.py's load_dotenv() may have just
# repopulated these from the developer's real .env.
for _k in ("FALLBACK_API_KEY", "GEMINI_API_KEY", "FALLBACK_2_API_KEY"):
    os.environ.pop(_k, None)
import translator as _translator_module  # noqa: E402
_translator_module._translator_instance = None


class NetworkBlocked(RuntimeError):
    """Raised by the neutered urlopen — a test tried to reach the network."""


def _blocked(*_a, **_k):
    raise NetworkBlocked("outbound network is disabled during tests")


urllib.request.urlopen = _blocked
_translator_module.urllib.request.urlopen = _blocked


@pytest.fixture(scope="session")
def app():
    """The real FastAPI app, imported once with credentials/network disabled."""
    return app_module.app


@pytest.fixture()
def client(app):
    from starlette.testclient import TestClient
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db_session():
    """A raw SQLAlchemy session for tests that need to seed rows directly
    (faster and more precise than going through the HTTP API for setup)."""
    from database import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture()
def fixtures_dir():
    return os.path.join(ROOT, "tests", "fixtures")


def load_fixture(name: str) -> str:
    path = os.path.join(ROOT, "tests", "fixtures", name)
    with open(path, encoding="utf-8") as fh:
        return fh.read()
