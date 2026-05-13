"""Exa neural/semantic search.

Used in Layer 3 for deep factor research. Returns conceptually related
content with full text, not just keyword matches.
"""

from __future__ import annotations

import os
from typing import Optional

import httpx

SEARCH_URL = "https://api.exa.ai/search"


class ExaError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.getenv("EXA_API_KEY")
    if not key:
        raise ExaError("EXA_API_KEY not set")
    return key


def search(
    query: str,
    *,
    num_results: int = 5,
    use_autoprompt: bool = True,
    search_type: str = "auto",
    start_published_date: Optional[str] = None,
    text: bool = True,
) -> dict:
    """Exa semantic search with content extraction.

    `text=True` asks Exa to return the page text so Layer 3 can read it
    without a separate fetch.
    """
    payload: dict = {
        "query": query,
        "numResults": num_results,
        "useAutoprompt": use_autoprompt,
        "type": search_type,
        "contents": {"text": True} if text else {},
    }
    if start_published_date:
        payload["startPublishedDate"] = start_published_date

    headers = {
        "x-api-key": _api_key(),
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=45.0) as client:
        response = client.post(SEARCH_URL, json=payload, headers=headers)
    if response.status_code != 200:
        raise ExaError(f"Exa search failed: {response.status_code} {response.text[:200]}")

    data = response.json()
    results = []
    for r in (data.get("results") or []):
        results.append({
            "title": r.get("title"),
            "url": r.get("url"),
            "published_date": r.get("publishedDate"),
            "author": r.get("author"),
            "score": r.get("score"),
            "text": (r.get("text") or "")[:6000],
        })
    return {
        "query": query,
        "autoprompt_string": data.get("autopromptString"),
        "results": results,
    }
