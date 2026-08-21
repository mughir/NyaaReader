"""
Scraper factory and registry — plugin architecture (auto-discovery).

Site-specific scrapers are PLUGINS. A plugin is just ONE file in this
directory containing a BaseScraper subclass with a `domains` list:

    class MySiteScraper(BaseScraper):
        domains = ["mysite.com"]

The registry imports every scrapers/*.py file, finds BaseScraper subclasses,
and registers each by its `domains`. There is NO registry to edit.

ACTIVATION (the only two things that matter)
--------------------------------------------
ACTIVE:     file exists in scrapers/  AND  the class has `domains` set.
DEACTIVATE: delete the file. Done — that domain falls back to AIScraper.

Any URL with no matching plugin falls back to AIScraper (LLM-powered
generic extraction), so a missing plugin never breaks anything.
"""
import importlib
import logging
import pkgutil
from typing import Optional

from scrapers.base import BaseScraper
from scrapers.ai import AIScraper

logger = logging.getLogger(__name__)


def _discover_plugins() -> dict:
    """Auto-discover every BaseScraper subclass in scrapers/*.py.

    Registration rule: a subclass is registered for each domain in its
    `domains` class attribute. Modules that fail to import (missing deps,
    private files stripped from a public build) are skipped silently.
    """
    import scrapers as pkg
    plugins = {}
    for mod_info in pkgutil.iter_modules(pkg.__path__):
        mod_name = mod_info.name
        if mod_name in ("base", "ai", "__init__"):
            continue
        if mod_name.startswith("_test_"):
            continue  # dev-only tests (run live network on import) — never load on startup
        try:
            mod = importlib.import_module(f"scrapers.{mod_name}")
        except Exception as e:
            logger.debug(f"skipping scrapers/{mod_name}.py: {e}")
            continue
        for attr in vars(mod).values():
            if (isinstance(attr, type) and issubclass(attr, BaseScraper)
                    and attr is not BaseScraper):
                domains = getattr(attr, "domains", None)
                if not domains:
                    continue
                for d in domains:
                    plugins[d] = attr
                    logger.info(f"plugin {attr.__name__} active for {d}")
    return plugins


SCRAPER_REGISTRY = _discover_plugins()


def get_scraper_for_url(url: str, **kwargs) -> Optional[BaseScraper]:
    """Get the plugin scraper for a URL, or the AIScraper fallback if none matches.
    Returns None only if even the AI fallback can't be built (no API key)."""
    from urllib.parse import urlparse

    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]

    for pattern, scraper_class in SCRAPER_REGISTRY.items():
        # Exact or subdomain match only — substring matching would let
        # evil-novel543.com hijack a plugin registered for novel543.com.
        if domain == pattern or domain.endswith("." + pattern):
            return scraper_class(**kwargs)

    # Universal fallback: LLM-powered generic extraction
    logger.info(f"No plugin for {domain} — using AIScraper (LLM extraction)")
    try:
        return AIScraper(**kwargs)
    except Exception as e:
        logger.error(f"Could not build AIScraper fallback: {e}")
        return None


def get_supported_sites() -> list:
    """Get list of supported site domains (plugins only, not the AI fallback)."""
    return list(SCRAPER_REGISTRY.keys())


async def auto_detect_and_scrape(url: str, **kwargs):
    """Auto-detect scraper (plugin or AI fallback) and scrape novel info."""
    scraper = get_scraper_for_url(url, **kwargs)
    if not scraper:
        raise ValueError(f"No scraper available for {url}")
    async with scraper:
        return await scraper.get_novel_info(url)
