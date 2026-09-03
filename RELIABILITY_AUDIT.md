# NyaaReader Backend — Reliability Audit

Scope: `backend/main.py`, `backend/translator.py`, `backend/models.py`, `backend/database.py` (pure static reading; no files modified).
Severity: **HIGH** = would visibly break for a user / corrupt data / lose work. **MED** = intermittent or conditional failure. **LOW** = edge case / latency / hygiene.

---

## Concurrency & batch/job races (the return on your time)

**1. [HIGH] `_fetch_chapter_content_sync()` return value is discarded — fetched content silently lost in 3 batch paths.**
`main.py:2408-2428` returns the scraped content object but never writes it to the DB (it just `return result`). Yet three callers rely on a side effect that doesn't exist:
- `translate_to_end_bg` `main.py:876-881`: `_fetch_chapter_content_sync(ch.source_url)` then `db.refresh(ch)` with the comment *"fetch helper commits content"* — it does NOT. For chapters with no `original_content`, `ch.original_content` stays empty and it raises `"fetch failed — no content"`. The chapter stays untranslated forever; the job just logs a warning and bumps the counter, so the user sees a green "done" bar while chapters silently never get content/translation.
- `check_updates_bg` `main.py:1009-1012`: same broken pattern — newly added chapters get content discarded, `if ch.original_content` is always false, translation skipped silently.
- `_retry_failed_bg` `main.py:2152-2156`: same — failed chapters whose content was never fetched can never be retried.
- Contrast: `translate_ahead_bg` `main.py:2462-2465` **does** assign `ch.original_content = ch_data.content` and commit — proving the helper itself doesn't persist and the other three paths are just broken callers.
*Why it's strange:* the docstring/comment claims the helper commits; it doesn't. Fetch success is a no-op. This is the single most user-visible silent data-loss bug in the batch pipeline.

**2. [HIGH] Batch-endpoint guard is TOCTOU — two concurrent starts run duplicate background loops on the same novel.**
Each POST endpoint checks `_batch_running(novel_id)` / `_set_batch(...)` and then `background_tasks.add_task(bg_fn, ...)` in a separate request. E.g. `translate_to_end` `main.py:845-853`, `translate_ahead` `main.py:2492-2499`, `retranslate_match` `main.py:905-912`, `check_updates` `main.py:952-954`, `retry_failed` `main.py:1405-1415`, `retranslate_drift` `main.py:1356-1370`, `translate_titles` `main.py:1439-1448`, `retranslate` `main.py:1520-1526`. The check-and-queue is not atomic: two near-simultaneous requests can both observe "not running" and both enqueue a worker. Inside the workers, `_set_batch` correctly refuses a *second* owner for `translate_ahead_bg` (`main.py:2450-2454`), but the OTHER workers ignore `_set_batch`'s return value (`translate_to_end_bg` `main.py:870`, `_retry_failed_bg` `main.py:2146`, `check_updates_bg` `main.py:1004`) and run the full loop regardless. Result: two threads translating the same chapters concurrently, both bumping the same job row → corrupted `done` counter.

**3. [HIGH] `reference _bump_batch` is a read-modify-write across separate SQLite sessions — lost updates / counter corruption.**
`_bump_batch` `main.py:1997-2016` opens its own `SessionLocal()`, `SELECT`s the most-recent running job, does `job.done += done_inc` in Python, then `commit()`. Because each `_bump_batch` (and `_set_batch`/`_clear_batch`/`_watchdog_pass`) uses an independent DB connection with no row lock/atomic increment, two concurrent workers (finding #2) can both read `done=5`, both write `done=6`, dropping an increment. This is exactly the `done > total` / spinner-never-stops corruption the code comments say it's defending against (`main.py:1911-1913`, `2053-2056`, `2021-2022`) — but the defense is only *corrective* (clamp on read), not *preventive*. The `_batch_cache` dict (`main.py:1859`, mutated in `_set_batch`/`_bump_batch`/`_clear_batch`/`_watchdog_pass`/`batch_status`) is also shared across threads with no lock; benign under GIL for single field writes but the read-modify-write sequences sit on the same race.

**4. [MED] `_set_batch` stale-takeover vs. active worker: a >5-min relay stall can be taken over mid-run.**
`_set_batch` `main.py:1923-1935` treats any running job with `updated_at` older than 5 min as "crashed leftover" and takes over the row (`existing.kind/total/done` reset). But a *live* worker may legitimately stall longer than 5 minutes in a single slow relay call (the relay timeout is 180s × 2 retries = up to 6 min per chapter, `translator.py:657,637`), before it ever bumps. If a second batch starts during that window it hijacks the row, sets `done=0`, and a second worker starts iterating the same novel → duplicates + counter corruption. The watchdog (`main.py:2120`, `JOB_STALL_MINUTES=10`) is at least longer than the relay worst case, but `_set_batch`'s 5-min threshold is not.

**5. [MED] `_translate_chapter_bg` re-entrancy: no per-chapter lock → double translation + last-writer-wins.**
`_translate_chapter_bg` `main.py:2975-3000` re-queries the chapter and proceeds only `if not chapter.is_translated` (`main.py:2985`), but nothing holds a lock between the read and the translate-then-commit. If the same chapter is queued twice (user clicks "translate" while a translate-ahead/to-end batch is also covering it — which the TOCTOU in #2 makes likely), two threads both see `is_translated=False`, both pay the relay cost, both write different translations; the second commit wins. No unique-work guard.

**6. [LOW] Fetch+translate via `fetch_more_chapters` blocks the entire event loop.**
`fetch_chapters_range` `main.py:2503` is `async def` (so it runs on the event loop as a FastAPI `BackgroundTask`), but inside it calls the **synchronous, blocking** `translator.translate_chapter(...)` at `main.py:2556` (up to 6 min per chapter with retries). While it runs, every other HTTP request and background task on the same loop is frozen. The equivalent work in the batch paths uses sync `def` workers (threadpool) or `translater._translate_chapter_bg`-in-thread, so they don't block the loop — `fetch_chapters_range` is the odd one out. (`fetch_more_chapters` `main.py:1848-1854`, `fetch_more_page` `main.py:3033` are the entry points.)

---

## translator.py — fallback chain & empty-content handling

**7. [verified OK] `translate_short` correctly RAISES on empty/echoed output.**
`translator.py:91-94`: `if not result or result == text.strip(): raise RuntimeError(...)`. This is the behavior the skill notes demand so the fallback engages. `FallbackTranslator._run` (`translator.py:687-706`) walks primary→fallbacks and only returns a value whose `success` attr is truthy (or a non-object for `translate_short`). Confirmed good.

**8. [MED] `_run` returns `None` (not an error) when every translator raises for `translate_short`/`translate_with_memory`.**
`_run` `translator.py:704-706`: on total failure (e.g. both `OpenAIRelayTranslator`s raise, or the primary `translate_with_memory` raises), `result` stays `None` and `_run` returns `None`. The sync `translate` method instead returns a well-formed `TranslationResult(success=False)` (`translator.py:199-208`), so the `success` check in `_run` handles it — but `translate_short`/`translate_with_memory` have **no such object-wrapping**, so callers must null-check. Most do (`if t and t.strip()`, `if not result.success:`), but `_translate_chapter` `main.py:670-677` calls `translator.translate_with_memory(...)` then dereferences `result.success` at `main.py:679` — if the translator returns `None` there it's an uncaught `AttributeError` → 500 on the sync endpoint. Only reached when the relay is fully down, but the failure mode is a raw 500 instead of a clean "translation failed" message.

**9. [LOW] `get_translator()` can return `None`, and it's dereferenced without a guard in `_translate_chapter`.**
`translator.py:751-760` returns `None` if the relay can't be constructed (missing `FALLBACK_API_KEY`). `_translate_chapter` `main.py:670-671` does `translator = get_translator(); result = translator.translate_with_memory(...)` — `None.translate_with_memory` → AttributeError → 500 on `POST /api/chapters/{id}/translate` and swallowed as `last_error` in the bg path. Same root as #8: no defensive check that the translator exists after config changes.

**10. [LOW] `OpenAIRelayTranslator._generate` retries only on empty content, not on 429/5xx/network drops.**
`translator.py:637-670`: the `for attempt in range(2)` loop only re-attempts when content is empty; any `Exception` (HTTP error, json decode, timeout) sets `last_err` and falls through to the single raise — no retry on transient failures. Combined with the relay being the *last* fallback, a transient 5xx turns into a recorded chapter failure + retry-queue work rather than a same-call retry. Not a correctness bug, but leaves failures on the table the loop's intent suggests it wanted to handle.

**11. [LOW] `_memory_update_block` failure is silently swallowed and indistinguishable from a real empty response.**
`translator.py:451-454` returns `""` on exception; `translate_with_memory` `main.py:358-363` then treats it as "keep old memory." That's intended best-effort (comment says so), but it means a *persistent* memory-update failure (e.g. relay down on the second call) is silently invisible — the chapter translates but the accumulated novel memory (characters/terms/plot) silently stops updating with no log. If the AI memory is a core feature, this is a silent degradation worth logging.

**12. [verified OK] `compact_memory` and `_parse_memory_update` degrade gracefully.**
`translator.py:489-495` returns the original memory without raising; `_parse_memory_update` `translator.py:502-512` falls back to input memory on any JSON/regex failure. `_reapply_locks` (`translator.py:377-403`) correctly re-asserts user-locked entries after updates. No bug found.

---

## Auth & sessions

**13. [MED] Session cookies never expire server-side and logout cannot revoke an existing token.**
`_verify_session` `main.py:103-117` validates the HMAC signature + 30-day TTL from the cookie timestamp; there is **no server-side session store**. `logout` `main.py:200-203` only `delete_cookie` client-side. A session token copied before logout remains valid for up to 30 days and there is no way to invalidate it (no secret rotation, no revocation list). For a self-hosted app this is "acceptable" but it's a real gap: after a password change, all outstanding cookies still work.

**14. [MED] Session cookie lacks the `Secure` flag — transmitted in cleartext over plain HTTP.**
`login` `main.py:194`: `set_cookie(_COOKIE_NAME, token, max_age=..., httponly=True, samesite="lax")` — no `secure=True`. This app is explicitly designed to be deployed publicly on a VPS (comments at `main.py:145-148`). If served over plain HTTP (the default for a self-hosted box), the bearer session cookie is sniffable, enabling session hijacking. The auth is also symmetric-HMAC-secret based with the secret stored in `DATA_DIR`; if `DATA_DIR` (line 88-95) isn't writable at import, the app fails to start.

**15. [LOW] Brute-force lockout is per-socket-IP, not per-real-client behind a reverse proxy.**
`_login_guard_*` `main.py:50-72` key on `request.client.host` (`main.py:186`). Behind nginx/Caddy (the typical VPS deployment for this app) every request arrives from the proxy's loopback/may be the proxy IP, so the lockout lumps all clients together (a distributed attacker or a full-IP DoS can lock out the legitimate user) or, without `X-Forwarded-For` handling here, fails to distinguish real IPs. It's also an in-memory dict — lost on restart, so a sustained attack restarts the lockout clock. Low severity for a self-hosted single-user app, but the per-IP premise is shaky in the deployment this app targets.

**16. [LOW] `auth_guard` opens a fresh DB session on every request, including every static asset.**
Middleware `main.py:163-181` calls `_auth_enabled()` (→ `_get_config()` → new `SessionLocal()`, `main.py:2235-2263`) on **every** request, before deciding auth is even off. With `app.mount("/static", ...)` traffic this is a DB session open+close per static file. Not a leak (closed in `finally`), but needless per-request DB churn that scales linearly with page-asset count. The `db` parameter threaded through `_auth_enabled`/`_auth_password` (`main.py:75-81`) is never actually used — both always fall back to `_get_config()`.

**17. [LOW] First-run `AppConfig(id=1)` creation is racy.**
`_get_config` `main.py:2242-2250`: if two threads both find no singleton row and both `db.add(AppConfig(id=1))`, the second commit raises a primary-key `IntegrityError`, uncaught → 500. Reachable only at the very first request cluster after a fresh DB (or after `auth_password` was set for the first time via two simultaneous logins). Rare, but a concrete race.

---

## Data consistency

**18. [MED] `total_chapters` drifts away from the real chapter count.**
It's set once from the scrape at add time (`main.py:410`), corrected on manual chapter add (`main.py:370`) and on `check_updates_bg` (`main.py:1000`), but **never reconciled** by any fetch/translate path or any periodic pass. `_create_novel_from_url` trusts `novel_info.total_chapters` from the scraper (`main.py:410`), which frequently disagrees with the actual rows the scraper's own `chapters` list created (`main.py:423-431`). The result is a library/reader "total chapters" header that disagrees with the chapter table. Cosmetic-ish but it's a declared field that drifts from ground truth.

**19. [verified OK] Glossary double-encoding is handled — not a live bug.**
`_load_glossary` `main.py:583-627` tolerates legacy string-encoded JSON (`isinstance(..., str) → json.loads`), and `_dump_glossary` `main.py:629-635` deliberately returns the list *without* `json.dumps` because the column is SQLAlchemy `JSON` (`models.py:133`) which serializes natively. The header comment explicitly documents the historical double-encode trap. Correct as written; no change needed.

**20. [MED] `chapter_page` marks "read" without any write-path guard, and read/translated derived counts are recomputed on the fly — no persisted drift source found, but is_read flip is on a read-only GET.**
`chapter_page` `main.py:2860-2863` does a `db.commit()` inside a `GET /novel/{id}/chapter/{n}` handler (side-effecting GET — a reload or prefetch marks read; repeated GETs also compare `chapter.read_at is None` so re-opening resets it unnecessarily once it's been viewed). Not a correctness bug per se, but a GET that writes is a classic foot-gun: any bot/prefetch touching the page flips `is_read`, and "read count" derived from `is_read` is therefore unreliable.

**21. [LOW] Memory auto-compaction can loop every chapter (never converges), doubling relay spend.**
`_translate_chapter` `main.py:707-720` compacts whenever `memory.needs_compaction()` (`translator.py:587-592`, threshold 6000 chars). `translate_with_memory` re-applies locked entries after the update (`main.py:367`, `_reapply_locks` `translator.py:377-403`), and `_reapply_locks` can *re-add* entries into `glossary_entries`/rebuild `characters`/`terms` (also counted by `needs_compaction`). If the locked glossary itself is near the budget, every single chapter can trip "over budget → compact" (an extra relay call per chapter) without ever dropping below threshold. The compaction code has no "am I making progress" guard. MED for relay cost, LOW for correctness.

**22. [verified OK] `update_progress`/`get_progress` — single-row-per-novel semantics are self-consistent.**
`models.py:71` makes `ReadingProgress.chapter_id` globally unique, and both handlers filter by `novel_id` and take `.first()` (`main.py:1558,1590`). The code only ever maintains one progress row per novel. No bug.

**23. [verified OK] Search and drift-count handle NULL translated content safely.**
`search_novel` `main.py:1632-1635` uses `translated_content.contains(q)` (SQL `LIKE` — NULL-safe) and null-guards the snippet; `drift_count`/`_retranslate_drift_bg` filter `translated_content.isnot(None)` (`main.py:1320,1364`). No NPE on untranslated chapters.

---

## Silent error-swallows worth flagging (only the user-visible ones)

- **#1's discarded fetch** is the biggest silent swallow — it logs a warning but presents "done."
- **`_translate_chapter_bg` swallows `HTTPException` into `last_error`** (`main.py:2991-2994`) — intentional and good (feeds the retry queue), but note `retry_failed` only retries rows with `is_translated=False AND last_error != ""` (`main.py:1407-1412`), so a chapter that WAS translated but whose memory-update failed has no `last_error` and no retry path — the memory drift in #11 is unrecoverable via the retry UI.
- `_export_epub_bg` failures and `run_backup` fallback (`main.py:2221-2223`) silently degrade to plain copy — acceptable, not flagged as bugs.
- `upload_cover` old-file unlink (`main.py:1181-1184`) and `scraper.__aexit__` (`main.py:2577-2578`) `except: pass` are benign cleanup — correctly not flagged.

---

## Priority summary (what to fix first)
1. **#1** — make `_fetch_chapter_content_sync` persist (or assign+commit at the 3 call sites) so to-end/updates/retry-failed actually fetch content. Highest user impact.
2. **#2/#3** — serialize batch starts atomically (a per-novel threading.Lock around the `_batch_running`/`_set_batch`/`add_task` block) and make `_bump_batch` an atomic `UPDATE ... SET done = done + :inc` instead of read-modify-write, so `done` can't corrupt.
3. **#4** — raise/instrument `_set_batch`'s takeover threshold so a live slow worker isn't hijacked.
4. **#6/#8/#9** — push `translate_chapter` off the event loop (run in executor), and null-guard `get_translator()` / `result` before dereference in `_translate_chapter`.
5. **#13/#14** — add `Secure` on the cookie when behind TLS and decide on session revocation (or at minimum rotate `_SESSION_SECRET` + document the 30-day window).
