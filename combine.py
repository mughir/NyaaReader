#!/usr/bin/env python3
"""
combine.py — combine the PUBLIC NyaaReader with the PRIVATE scraper vault.

NyaaReader is split into two folders so the public repo stays clean (no
redaction engineering, no private material):
  1. <repo>/          — the PUBLIC project (NyaaReader) — clean code + README
  2. <private>/       — your PRIVATE vault (nyaareader-scrapper): private_*.py

The public tree never touches the private plugins, so it can be pushed to
GitHub as-is. This script is the ONLY place that merges them — it copies the
private scraper plugins into the working <repo>/scrapers/ directory so your
personal build/deploy has the private site plugins installed, while the
public repo stays clean.

Usage:
    python combine.py                 # use default private vault (../nyaareader-scrapper)
    python combine.py <private_dir>   # point at a specific private vault

Exit code 0 on success. Prints what it copied.
"""
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent        # the public NyaaReader tree
DEFAULT_VAULT = REPO.parent / "nyaareader-scrapper"

# Files that live in the private vault and must be overlaid onto the public
# scrapers/ at build time. (Prefix match: any of these under scrapers/)
PRIVATE_MARKERS = ("private_", "_test_")

def _private_files(vault: Path) -> list:
    """All private scraper files in the vault's scrapers/ dir."""
    scrapers = vault / "scrapers"
    if not scrapers.is_dir():
        print(f"!! no {scrapers.name}/ in vault {vault}")
        return []
    out = []
    for f in sorted(scrapers.iterdir()):
        if f.is_file() and f.suffix == ".py" and any(f.name.startswith(m) for m in PRIVATE_MARKERS):
            out.append(f)
    return out

def main() -> int:
    vault = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_VAULT
    if not vault.is_dir():
        print(f"private vault not found: {vault}")
        print("pass the path, e.g.  python combine.py ../nyaareader-scrapper")
        return 2

    src = REPO / "scrapers"
    src.mkdir(parents=True, exist_ok=True)

    files = _private_files(vault)
    if not files:
        print(f"no private scraper files found in {vault / 'scrapers'}")
        return 3

    for f in files:
        dest = src / f.name
        shutil.copy2(f, dest)
        print(f"  + {f.name}")

    print(f"\nCombined {len(files)} private plugin(s) into {src}")
    print("You can now build/run with your private site plugins installed.")
    print("(The public git repo ignores scrapers/private_*.py, so nothing leaks.)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
