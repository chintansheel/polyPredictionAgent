"""Tavily client — search + extract.

Uses the Tavily HTTP API directly (small surface; one dep less).
Both tools return JSON-serializable dicts that can be passed straight
back to Claude as a tool_result.
"""

from __future__ import annotations

import os
from typing import Optional

import httpx

SEARCH_URL = "https://api.tavily.com/search"
EXTRACT_URL = "https://api.tavily.com/extract"


class TavilyError(RuntimeError):
    pass


def _api_key() -> str:
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        raise TavilyError("TAVILY_API_KEY not set")
    return key


def search(
    query: str,
    *,
    max_results: int = 5,
    search_depth: str = "advanced",
    include_answer: bool = True,
    days: Optional[int] = None,
    topic: str = "news",
) -> dict:
    """Tavily search — returns dict with 'answer' and 'results' list."""
    payload: dict = {
        "api_key": _api_key(),
        "query": query,
        "max_results": max_results,
        "search_depth": search_depth,
        "include_answer": include_answer,
        "topic": topic,
    }
    if days is not None:
        payload["days"] = days

    with httpx.Client(timeout=30.0) as client:
        response = client.post(SEARCH_URL, json=payload)
    if response.status_code != 200:
        raise TavilyError(f"Tavily search failed: {response.status_code} {response.text[:200]}")

    data = response.json()
    return {
        "query": query,
        "answer": data.get("answer"),
        "results": [
            {
                "title": r.get("title"),
                "url": r.get("url"),
                "content": r.get("content"),
                "score": r.get("score"),
                "published_date": r.get("published_date"),
            }
            for r in (data.get("results") or [])
        ],
    }


# A successful HTTP response from Tavily can still hand back an empty body
# (e.g. paywalled, JS-rendered, or anti-bot pages). Counting that as a
# "success" misleads downstream: the agent will happily cite the URL even
# though no content was actually read from it. We require a minimum amount
# of raw text before treating an extract as content-bearing.
EXTRACT_MIN_CONTENT_CHARS = 100


def extract(urls: list[str] | str) -> dict:
    """Tavily extract — pulls full article content from one or more URLs.

    Returns a dict including a `content_retrieved` flag and a `success`
    flag that reflects *whether content was actually retrieved*, not just
    whether the HTTP call succeeded. Downstream span instrumentation
    relies on this distinction.
    """
    if isinstance(urls, str):
        urls = [urls]
    if not urls:
        raise TavilyError("extract() requires at least one URL")

    payload = {
        "api_key": _api_key(),
        "urls": urls,
    }
    with httpx.Client(timeout=45.0) as client:
        response = client.post(EXTRACT_URL, json=payload)
    if response.status_code != 200:
        raise TavilyError(f"Tavily extract failed: {response.status_code} {response.text[:200]}")

    data = response.json()
    results = [
        {
            "url": r.get("url"),
            "raw_content": (r.get("raw_content") or "")[:8000],
        }
        for r in (data.get("results") or [])
    ]

    first_raw = results[0]["raw_content"] if results else ""
    content_retrieved = bool(results) and len(first_raw) >= EXTRACT_MIN_CONTENT_CHARS

    return {
        "urls": urls,
        "results": results,
        "failed_results": data.get("failed_results") or [],
        "success": content_retrieved,
        "content_retrieved": content_retrieved,
        "output_items": len(results),
        "raw_content": first_raw,
    }
