"""
Hang-regression test: while a SLOW AI translation is in flight, the app must
stay responsive. This is the regression class fixed 2026-08-27 (blocking
translator + blocking _novel_lock on the event loop).

Boots the real FastAPI app on a scratch DB, monkeypatches the translator with
an 8-second-stall fake, then measures /api/health + /api/novels latency while
POST /api/chapters/{id}/translate is running.
"""
import os, sys, time, threading, json, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))
os.chdir(ROOT)

os.environ["DATABASE_URL"] = "sqlite:///./data/test_hang.db"
os.environ["DATA_DIR"] = "data"
for suffix in ("", "-wal", "-shm"):
    p = f"data/test_hang.db{suffix}"
    if os.path.exists(p):
        os.remove(p)

import main as appmod
from database import SessionLocal, init_db
from models import Novel, Chapter

init_db()


class FakeSlowTranslator:
    """Stands in for the relay: translate_with_memory stalls 8s (slow AI)."""

    def translate_with_memory(self, *a, **k):
        time.sleep(8)
        res = appmod.TranslationResult(
            success=True, translated_text="translated text", model_used="fake",
            output_tokens=10, input_tokens=10, estimated_cost=0.0,
        )
        res.memory = None  # MemoryTranslationResult carries one; plain result may not
        return res

    def translate_short(self, *a, **k):
        return "fake title"

    def compact_memory(self, mem, *a, **k):
        return mem


appmod.get_translator = lambda *a, **k: FakeSlowTranslator()

# Seed one novel + one translatable chapter
db = SessionLocal()
novel = Novel(title="HangTest", source_url="manual://hangtest", source_site="manual",
              original_language="zh", target_language="en", total_chapters=1)
db.add(novel)
db.flush()
ch = Chapter(novel_id=novel.id, chapter_number=1, title="c1",
             source_url="", original_content="原始内容 " * 500, is_translated=False)
db.add(ch)
db.commit()
NOVEL_ID, CH_ID = novel.id, ch.id
db.close()

import uvicorn
from threading import Thread

t = Thread(target=lambda: uvicorn.run(appmod.app, host="127.0.0.1", port=8099,
                                      log_level="warning"), daemon=True)
t.start()


def req(method, path, timeout=30):
    r = urllib.request.Request(f"http://127.0.0.1:8099{path}", method=method)
    t0 = time.time()
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        resp.read()
    return time.time() - t0, resp.status


# wait for boot
for _ in range(60):
    try:
        req("GET", "/api/health", timeout=2)
        break
    except Exception:
        time.sleep(0.5)
else:
    print("FAIL: app never became healthy")
    sys.exit(1)
print("boot ok")

results = {}

# --- Test 1: slow chapter-translate in flight; app must stay responsive ---
def slow_translate():
    body = json.dumps({"chapter_id": CH_ID}).encode()
    r = urllib.request.Request(f"http://127.0.0.1:8099/api/chapters/{CH_ID}/translate",
                               data=body, method="POST",
                               headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            resp.read()
        results["translate_ok"] = True
    except Exception as e:
        results["translate_ok"] = False
        results["translate_err"] = str(e)
    results["translate_s"] = round(time.time() - t0, 1)

th = Thread(target=slow_translate)
th.start()
time.sleep(1.5)  # let the translate enter its 8s stall

h1, s1 = req("GET", "/api/health", timeout=10)
h2, s2 = req("GET", "/api/novels", timeout=10)
h3, s3 = req("GET", "/api/stats", timeout=10)
print(f"during slow translate: health={h1:.2f}s novels={h2:.2f}s stats={h3:.2f}s")

# --- Test 2: config health-check to a dead endpoint; app must stay responsive ---
def dead_healthcheck():
    body = json.dumps({"base_url": "http://127.0.0.1:9/v1", "api_key": "x"}).encode()
    r = urllib.request.Request("http://127.0.0.1:8099/api/config/health-check",
                               data=body, method="POST",
                               headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            resp.read()
        results["healthcheck_ok"] = True
    except Exception as e:
        results["healthcheck_ok"] = False
        results["healthcheck_err"] = str(e)
    results["healthcheck_s"] = round(time.time() - t0, 1)

th2 = Thread(target=dead_healthcheck)
th2.start()
time.sleep(0.8)

h4, s4 = req("GET", "/api/health", timeout=10)
print(f"during dead-endpoint health-check: health={h4:.2f}s")

th.join(timeout=30)
th2.join(timeout=30)

ok = True
if h1 > 2.0 or h2 > 2.0 or h3 > 2.0:
    print("FAIL: app stalled during slow translate (>2s responses)")
    ok = False
if h4 > 2.0:
    print("FAIL: app stalled during config health-check")
    ok = False
if not results.get("translate_ok"):
    print("FAIL: translate errored:", results.get("translate_err"))
    ok = False
if not results.get("healthcheck_ok"):
    print("FAIL: health-check errored:", results.get("healthcheck_err"))
    ok = False
print("translate took", results.get("translate_s"), "s; healthcheck took",
      results.get("healthcheck_s"), "s")
print("PASS: app stayed responsive during slow AI" if ok else "FAIL")
sys.exit(0 if ok else 1)
