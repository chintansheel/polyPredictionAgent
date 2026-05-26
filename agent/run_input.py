"""Parse user input for a one-off analysis: keywords vs Polymarket links."""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse


def parse_keyword_or_polymarket_url(text: str) -> tuple[Optional[str], Optional[str]]:
    """Return ``(keyword_filter, market_id_override)`` for :func:`run_once`.

    Exactly one of the tuple entries should be non-``None`` for a valid run
    (keyword search vs direct market/event slug lookup on Gamma).

    Polymarket event URLs look like
    ``https://polymarket.com/event/<slug>``; market URLs may use
    ``/market/<id-or-slug>``.
    """
    raw = (text or "").strip()
    if not raw:
        return None, None

    if "polymarket.com" not in raw.lower():
        return raw, None

    url = raw
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url.lstrip("/")

    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if host.endswith(":443"):
        host = host[:-4]
    if "polymarket.com" not in host:
        return None, None

    parts = [p for p in parsed.path.strip("/").split("/") if p]
    if not parts:
        return None, None

    if parts[0] == "event" and len(parts) >= 2:
        return None, parts[1]
    if parts[0] == "market" and len(parts) >= 2:
        return None, parts[1]

    return None, None
