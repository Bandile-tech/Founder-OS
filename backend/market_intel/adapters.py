"""
Source adapters for the Market Intelligence Agent.

Every adapter implements ``search(query, limit) -> list[dict]`` returning evidence
items shaped as::

    {"source_type": <adapter name>, "ref": <url|path|doc ref>,
     "title": str, "excerpt": str}

Contract: an adapter must NEVER raise — it degrades to an empty list on any
failure (network down, table empty, malformed HTML). The agent core only ever
iterates ``ADAPTERS``; adding a new source is a new class plus one registry
entry — no core changes.

Adapters are called from parallel researcher threads, so each opens its own DB
session rather than sharing the request session.
"""

import re
import html as html_lib

import httpx

_EXCERPT_LEN = 400


def _clip(text: str) -> str:
    text = " ".join((text or "").split())
    return text[:_EXCERPT_LEN] + ("…" if len(text) > _EXCERPT_LEN else "")


class SourceAdapter:
    """Interface. Subclasses set ``name`` and implement ``_search``."""

    name = "base"

    def search(self, query: str, limit: int = 5) -> list:
        if not query or len(query.strip()) < 2:
            return []
        try:
            return self._search(query.strip(), limit)[:limit]
        except Exception as e:              # noqa: BLE001 — degrade, never crash a run
            print(f"[market_intel] adapter '{self.name}' failed for '{query}': {e}")
            return []

    def _search(self, query: str, limit: int) -> list:
        raise NotImplementedError


class WebSearchAdapter(SourceAdapter):
    """Web research via the DuckDuckGo HTML endpoint (no API key, no new deps).

    Covers open-web research, industry reports, reviews, forums, and public
    discussions — the Planner steers coverage by writing channel-specific
    queries (e.g. ``site:reddit.com``, ``"report"``, ``review``) against this
    one transport.
    """

    name = "web"
    _URL = "https://html.duckduckgo.com/html/"
    # Anchor + snippet pairs in DDG's html results page.
    _RESULT_RE = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    _SNIPPET_RE = re.compile(
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        re.DOTALL,
    )

    def _search(self, query: str, limit: int) -> list:
        resp = httpx.post(
            self._URL,
            data={"q": query},
            headers={"User-Agent": "FounderOS-MarketIntel/1.0"},
            timeout=10.0,
            follow_redirects=True,
        )
        resp.raise_for_status()
        page = resp.text

        links = self._RESULT_RE.findall(page)
        snippets = self._SNIPPET_RE.findall(page)

        results = []
        for i, (href, title_html) in enumerate(links[:limit]):
            title = html_lib.unescape(re.sub(r"<[^>]+>", "", title_html)).strip()
            snippet = ""
            if i < len(snippets):
                snippet = html_lib.unescape(re.sub(r"<[^>]+>", "", snippets[i])).strip()
            results.append({
                "source_type": self.name,
                "ref": html_lib.unescape(href),
                "title": title,
                "excerpt": _clip(snippet or title),
            })
        return results


class WarRoomAdapter(SourceAdapter):
    """Keyword search over War Room doctrine documents (documents/document_chunks).

    Queries the tables directly with its own session — same matching rule as
    ``/context/search`` but thread-safe and without the HTTP error semantics.
    """

    name = "war_room"

    def _search(self, query: str, limit: int) -> list:
        from database import SessionLocal
        import models

        db = SessionLocal()
        try:
            q_lower = query.lower()
            rows = (
                db.query(models.DocumentChunk, models.Document.title)
                .join(models.Document, models.DocumentChunk.document_id == models.Document.id)
                .all()
            )
            results = []
            for chunk, doc_title in rows:
                if q_lower in (chunk.content or "").lower():
                    results.append({
                        "source_type": self.name,
                        "ref": f"document:{chunk.document_id}#chunk{chunk.chunk_index}",
                        "title": doc_title,
                        "excerpt": _clip(chunk.content),
                    })
                    if len(results) >= limit:
                        break
            return results
        finally:
            db.close()


class ColdArchiveAdapter(SourceAdapter):
    """Keyword search over the Obsidian vault index (cold_archive_chunks).

    The agent is an explicitly-commanded research run, so reading the vault
    here does not violate the cold-storage rule — the user initiated it.
    """

    name = "cold_archive"

    def _search(self, query: str, limit: int) -> list:
        from database import SessionLocal
        from vault_sync import search_vault

        db = SessionLocal()
        try:
            raw = search_vault(query, db, limit=limit)
        finally:
            db.close()
        return [
            {
                "source_type": self.name,
                "ref": r["file_path"],
                "title": r["file_title"],
                "excerpt": _clip(r["content"]),
            }
            for r in raw
        ]


ADAPTERS = {
    a.name: a
    for a in (WebSearchAdapter(), WarRoomAdapter(), ColdArchiveAdapter())
}


def search_all(query: str, limit_per_source: int = 5) -> list:
    """Run one query across every registered adapter and pool the evidence."""
    evidence = []
    for adapter in ADAPTERS.values():
        evidence.extend(adapter.search(query, limit=limit_per_source))
    return evidence
