#!/usr/bin/env python3
"""
make_public.py — build a public-ready copy of novel-reader.

Removes everything that must stay private:
  - scrapers/private_*.py        (novel543, syosetu — site-specific plugins)
  - backend/.env, root .env      (API keys)
  - HANDOFF.md, STATUS.md        (internal session notes)
  - benchmark_* scripts+results  (relay-key history + internal pricing analysis)
  - data/, backups               (DB with real content)
  - git history                  (fresh repo; old history contains earlier key versions)

Usage:  python make_public.py [output_dir]   (default: ../novel-reader-public)

After running: cd <output_dir> && git init && git add -A && git commit,
then push to your public GitHub repo. The registry auto-detects the missing
private plugins and routes those sites to the AI fallback scraper.
"""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = ROOT.parent / "novel-reader-public"

SKIP_NAMES = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    "data", "backups",
}
SKIP_FILES = {
    ".env", "HANDOFF.md", "STATUS.md",
}
SKIP_PREFIXES = ("benchmark_", "rate_models.py", "summarize_bench.py",
                 ".tmpprobe", "novel_reader.db", ".db-wal", ".db-shm")
PRIVATE_SCRAPERS = {"private_novel543.py", "private_syosetu.py"}
# ALL site plugins stay private — the public build ships AI scraper + plugin
# system only (registry tolerantly falls back to AIScraper for every URL).
SITE_PLUGINS = {"chinese.py", "japanese.py", "korean.py"}
# private-site regression tests reference the excluded plugins
PRIVATE_TEST_FILES = {"_test_novel543.py"}


def should_skip(name: str, rel: Path) -> bool:
    if name in SKIP_NAMES or name in SKIP_FILES:
        return True
    if name in PRIVATE_SCRAPERS or name in PRIVATE_TEST_FILES or name in SITE_PLUGINS:
        return True
    if any(name.startswith(p) for p in SKIP_PREFIXES):
        return True
    return False


def copy_tree(src: Path, dst: Path):
    for item in src.iterdir():
        rel = item.relative_to(src)
        if item.is_dir():
            if item.name in SKIP_NAMES:
                continue
            copy_tree(item, dst / rel)
        else:
            if should_skip(item.name, rel):
                continue
            (dst / rel).parent.mkdir(parents=True, exist_ok=True)
            if item.name == "README.md":
                _write_public_readme(item, dst / rel)
            elif item.name == "make_public.py":
                _write_public_make_public(item, dst / rel)
            else:
                shutil.copy2(item, dst / rel)


def _write_public_make_public(src: Path, dst: Path):
    """make_public.py ships in the public repo — strip the private-scraper
    filenames from its own source so the public copy doesn't reveal them."""
    text = src.read_text(encoding="utf-8")
    text = text.replace("(novel543, syosetu — site-specific plugins)", "(undisclosed private plugins)")
    text = text.replace('PRIVATE_SCRAPERS = {"private_novel543.py", "private_syosetu.py"}',
                        'PRIVATE_SCRAPERS = {"private_*.py"}  # names redacted for the public build')
    text = text.replace('PRIVATE_TEST_FILES = {"_test_novel543.py"}',
                        'PRIVATE_TEST_FILES = set()  # redacted for the public build')
    # The REDACT tuple in _write_public_readme holds the private names —
    # neutralize it so the shipped file carries no trace.
    text = text.replace('REDACT = ("novel543", "syosetu")',
                        'REDACT = ("private1", "private2")  # redacted for the public build')
    dst.write_text(text, encoding="utf-8")


def _write_public_readme(src: Path, dst: Path):
    """Copy README.md but strip any mention of private/undisclosed site
    plugins (their names + domains stay out of the public repo)."""
    text = src.read_text(encoding="utf-8")
    # Redaction set — the private names are only referenced through this
    # opaque tuple so the public copy of THIS file never contains them.
    REDACT = ("novel543", "syosetu")
    n1, n2 = REDACT
    text = text.replace(f"+ private: {n1}, {n2}", "")
    text = text.replace(f" or a {n1}.com book page", "")
    text = text.replace(f"\u2502   \u251c\u2500\u2500 private_*.py    # {n1}, {n2} \u2014 EXCLUDED from public builds\n", "")
    import re as _re
    text = _re.sub(rf"private[:\s]+{n1},? {n2}", "undisclosed private sites", text)
    text = _re.sub(rf"{n1}\.com", "a supported site", text)
    text = _re.sub(rf"{n2}", "Syosetu", text)
    dst.write_text(text, encoding="utf-8")


def main():
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    # SYNC mode (not wipe-and-recreate): keep the existing .git so history
    # APPENDS and pushes are normal merges, never force-replace.
    # Only the initial run creates the dir (then: git init && git commit).
    if out.exists():
        _prune_removed(ROOT, out)
    else:
        out.mkdir(parents=True)
    copy_tree(ROOT, out)

    # Public README note about plugins
    (out / "PUBLIC_BUILD.md").write_text(
        "# Public build\n\n"
        "This is the public copy of NyaaReader. Site-specific scraper plugins\n"
        "from the author's private setup are intentionally excluded. Any URL\n"
        "without a bundled plugin automatically uses `scrapers/ai.py` — the\n"
        "LLM-powered fallback scraper that extracts novels/chapters from any\n"
        "site via the relay API.\n\n"
        "To add your own site: copy `scrapers/example_plugin.py`, set `domains`,\n"
        "implement the two methods — discovery registers it automatically.\n", encoding="utf-8")

    # .env template for public users (contains no secrets)
    if not (out / ".env.example").exists():
        (out / ".env.example").write_text(
            "# Copy to .env and fill in your keys.\n"
            "# Relay API key (opencode.ai zen) — the single key used by the\n"
            "# translation chain (deepseek-v4-flash -> gpt-5.6-luna) and the AI\n"
            "# fallback scraper.\n"
            "FALLBACK_API_KEY=your_relay_key_here\n", encoding="utf-8")

    count = sum(1 for _ in out.rglob("*") if _.is_file())
    print(f"Public tree synced to {out} ({count} files)")
    print("Next: cd there && git add -A && git commit && git push (appends history — no force)")


def _prune_removed(src: Path, dst: Path):
    """Delete files in dst that no longer exist in src (keeps the sync honest)."""
    for item in dst.iterdir():
        rel = item.relative_to(dst)
        if rel.parts[0] == ".git":
            continue
        src_item = src / rel
        if not src_item.exists():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
        elif item.is_dir():
            _prune_removed(src_item, item)


if __name__ == "__main__":
    main()
