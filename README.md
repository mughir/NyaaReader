# 🐾 NyaaReader

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

> *A self-hosted web novel reader that translates your stories on the fly. Powered by a very good cat named Nyaa. Nyaa~ 🐱*

NyaaReader is a cozy, self-hosted reader for Japanese / Chinese / Korean web novels. Paste a novel URL, and it scrapes the chapters and **auto-translates them to your language** — so you can binge a light novel in English (or whatever you read in) while it quietly preps the next chapters in the background. Runs in Docker on your own machine.

---

## ✨ What Nyaa can do

- 📖 **Multi-site scraping (plugin system)** — site-specific plugins (JJWXC, Qidian, 17K, Kakuyomu, AlphaPolis, Novelpia, Munpia, Naver, Ridibooks, and a few private ones) plus **`AIScraper`**, an LLM-powered generic fallback that extracts novels/chapters from *any* site without a plugin (using the same relay chain that translates). To add a site: copy `scrapers/example_plugin.py`, implement two methods, register your domain — done. Remove it to fall back to AI extraction. (Your private site plugins live in a separate private vault and are overlaid here at build time by `combine.py`.)
- 🌐 **AI translation, one key** — `deepseek-v4-flash` (best value) → `gpt-5.6-luna` (quality), both on a **single** relay key (opencode.ai zen). The quality tier kicks in only when the fast one fails. (Blind-benchmarked 10 models for score vs. cost before wiring.)
- 🧠 **Per-novel AI memory** — Nyaa remembers characters, terms, and plot across chapters, with **auto-compaction** so a 500+ chapter novel stays consistent without runaway cost.
- 🔒 **Editable glossary with locks** — keep names consistent (fix that one character you can't stand being retranslated differently). Locked entries are fed to the translator as **mandatory**.
- 🚀 **Translate-ahead** — open a chapter and the next raw ones quietly fetch+translate in the background, ready when you scroll to them. Prefer to read the whole thing now? **Translate-to-end** backfills the rest.
- 🏷 **Translate titles only** — a quick pass for missing chapter titles, no full re-run.
- 🎯 **Smart partial retranslate** — changed a locked name? Retranslate only the chapters that contain it, not all 500.
- 🆕 **New-chapter watcher** — `Check updates` polls the source for new chapters (politely, 15s apart).
- 📚 **Reading shelf** — Ongoing / Read Later / Done / Dropped, per-chapter read dots, resume-where-you-left.
- 🖍 **Highlights & bookmarks** — drag-select any paragraph to bookmark it with a note; jump back any time.
- 📄 **Bilingual hover** — hover a translated paragraph to peek at the original inline.
- 🔍 **Full-text search** — search inside translated chapters, not just titles.
- 🎨 **Reader comfort** — light/sepia/dark themes, serif/sans font, width slider, focus mode, keyboard shortcuts, swipe gestures, immersive auto-hiding toolbar.
- 🔐 **Password lock (optional)** — set one password in Settings and the whole app is behind a login. Leave it unset to run open locally.
- 💾 **Backups** — automatic + on-demand snapshots of your whole library (see below).

---

## 🚀 Quick Start (Docker)

> Requires **Docker Desktop** (running).

1. **Drop in your relay key** — create `.env` at the project root (gitignored):
   ```env
   FALLBACK_API_KEY=YOUR_RELAY_KEY   # opencode.ai zen — the ONE key for everything
   ```
   Grab a key at opencode.ai. Single-provider chain: `deepseek-v4-flash` → `gpt-5.6-luna`.

2. **Start Nyaa:**
   ```bash
   docker-compose up --build -d
   ```
   (or double-click `start.bat`)

3. **Open:** http://localhost:8080 🐱

4. **Add a novel:** paste a supported novel URL (novel543.com or syosetu.com — the first 5 chapters auto-fetch + translate in the background).

---

## 🛠 Manual run (no Docker)

> `scrapers/` lives at the repo root, not in `backend/` — set `PYTHONPATH`. **Docker is the supported path.**

```bash
export PYTHONPATH="$PWD:$PWD/backend"
cd backend
pip install -r requirements.txt
export FALLBACK_API_KEY=YOUR_RELAY_KEY
python main.py
```
Then open http://localhost:8080

---

## 📁 Project structure

```
NyaaReader/
├── backend/            # FastAPI + SQLite + translator
│   ├── main.py         # All API routes + server-rendered Vue pages
│   ├── models.py       # Novel, Chapter, ReadingProgress, NovelSettings, NovelMemory, Bookmark, DiaryEntry
│   ├── translator.py   # Relay chain (deepseek-v4-flash → gpt-5.6-luna), memory, compaction
│   ├── database.py     # SQLite + lightweight ALTER-TABLE migrations
│   └── .env            # Secrets (gitignored)
├── frontend/           # Served by backend at /static
│   ├── library.js      # Library page (Vue): shelf tabs, cards, cover fallback
│   ├── novel.js        # Novel page (Vue): search/filter/pager, glossary editor, batch progress
│   ├── reader.js       # Reader page (Vue): TOC, bookmarks, bilingual hover, settings
│   ├── review.js       # Review page (Vue): story-so-far + reading diary
│   ├── config.js       # Settings page (Vue): API keys, fallback models, backups
│   ├── favicon.svg     # The official cat
│   ├── styles.css      # Themes & typography
│   └── vendor/         # Vendored Vue 3 (no CDN dependency)
├── scrapers/           # Plugin registry + site plugins (private_*.py NOT tracked here)
│   ├── ai.py           # AIScraper — LLM fallback for sites without a plugin
│   ├── example_plugin.py  # Template for writing your own site plugin
│   └── chinese.py / japanese.py / korean.py  # site plugins
├── combine.py          # Overlays your private plugins from the private vault at build time
├── LICENSE             # MIT — attribution required (credit this repo)
├── Dockerfile
├── docker-compose.yml  # Secrets via ${VAR} from root .env
└── README.md           # Project docs (STATUS/HANDOFF are local-only session notes)
```

> **Two-folder split:** this repo is the **public** NyaaReader. Your **private** site-scraper plugins
> live in a separate private vault (`nyaareader-scrapper`) and are overlaid into `scrapers/` **only at
> build time** by `combine.py` (and `start.bat` runs it for you). The public repo therefore never
> contains or reveals them — no README redaction needed. See `combine.py`.

---

## 📡 API overview

| Method | Path | Description |
|--------|------|-------------|
| GET  | `/api/health` | Health check |
| GET/POST | `/api/novels` | List / add novel by URL |
| POST | `/api/novels/manual` | Add novel manually (no scraper) |
| GET  | `/api/novels/{id}` | Novel detail |
| GET  | `/api/novels/{id}/chapters` | Chapter list |
| GET  | `/api/novels/{id}/chapters/{n}` | Chapter content |
| POST | `/api/novels/{id}/chapters/manual` | Add a chapter manually |
| POST | `/api/chapters/{id}/translate` | Translate a chapter (memory-aware) |
| POST | `/api/novels/{id}/chapters/{n}/fetch` | Fetch one chapter |
| POST | `/api/novels/{id}/fetch-chapters` | Fetch a range (background) |
| POST | `/api/novels/{id}/translate-ahead` | Queue next raw chapters |
| POST | `/api/novels/{id}/translate-to-end` | Backfill the whole remaining novel |
| POST | `/api/novels/{id}/translate-titles` | Translate missing chapter titles only |
| POST | `/api/novels/{id}/translate-meta` | Translate novel title + description |
| POST | `/api/novels/{id}/retranslate` | Re-translate all translated chapters |
| POST | `/api/novels/{id}/retranslate-match` | Re-translate only chapters containing a string |
| POST | `/api/novels/{id}/check-updates` | Check source for new chapters |
| GET  | `/api/novels/{id}/memory` | AI memory (characters/terms/plot) |
| PUT  | `/api/novels/{id}/memory` | Save glossary entries + locks |
| GET/POST | `/api/novels/{id}/progress` | Reading progress |
| PUT  | `/api/novels/{id}/reading-status` | Shelf: ongoing/read_later/done/dropped |
| GET/PUT | `/api/novels/{id}/settings` | Per-novel settings |
| GET  | `/api/novels/{id}/bookmarks` | List bookmarks |
| POST | `/api/chapters/{id}/bookmarks` | Save a bookmark/highlight |
| DELETE | `/api/bookmarks/{id}` | Remove a bookmark |
| POST | `/api/novels/{id}/search` | Full-text search in translated content |
| GET  | `/api/novels/{id}/batch-status` | Background job progress |
| DELETE | `/api/novels/{id}` | Remove novel |
| GET  | `/api/stats` | Library stats |
| GET  | `/api/auth/status` | Whether password login is enabled |

Interactive API docs: http://localhost:8080/docs

---

## 🧠 Translation chain & cost

- **Tier 1:** `deepseek-v4-flash` — best value (78 score / ~$0.00085 per chapter).
- **Tier 2:** `gpt-5.6-luna` — quality (85 / ~$0.00276), engages only when tier 1 fails.
- **Single key:** both tiers use one `FALLBACK_API_KEY` (opencode.ai zen relay).
- Config in env: `FALLBACK_MODEL`, `FALLBACK_MODEL_2` (in `.env` / `docker-compose.yml`).

---

## 💾 Backups

Your whole library (novels, chapters, translations, memory, settings) is a single SQLite file (`data/novel_reader.db`). Nyaa keeps snapshots of it for you.

**Settings → Backups:**

| Wanna… | Do this |
|---|---|
| **Back up now** | Click `💾 Backup now` — a consistent copy lands in `data/backups/` |
| **Back up automatically** | Tick "Automatic backups", set **Interval (hours)** and **Keep last N** (old ones prune themselves) |
| **Download a copy** | Click `⬇` on a row to save the `.db` to your computer |
| **Delete a backup** | Click `🗑` on a row (after a confirm) to remove that snapshot |
| **Restore** | Stop the app, replace `data/novel_reader.db` with the backup file, restart. |

> Backups are raw SQLite snapshots — grab one before big changes for an easy rollback.

---

## 📄 License

Released under the [MIT License](LICENSE). You're free to use, copy, modify, and distribute it as long as you keep the copyright notice, the attribution line, and the license text — **anyone copying this code must credit the original repo** (`https://github.com/mughir/NyaaReader`). See `LICENSE` for the full text.

---

*NyaaReader is not affiliated with any of the source sites. It exists for your own reading convenience — please respect each site's terms and the authors' work. Nyaa~ 🐾*
