"""Minimal outbound HTTPS webhook delivery for Dyops escalations."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from urllib.parse import urlsplit

import httpx

_TIMEOUT_SECONDS = 2.0
_LOGGER = logging.getLogger(__name__)


def configured_urls() -> tuple[str, ...]:
    """Return non-empty URLs from ``DYOPS_WEBHOOK_URLS``."""
    return tuple(
        url.strip()
        for url in os.environ.get("DYOPS_WEBHOOK_URLS", "").split(",")
        if url.strip()
    )


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
) -> None:
    for attempt in range(2):
        try:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            target = urlsplit(url)
            _LOGGER.info(
                "Dyops webhook delivered: level=%s instrument=%s target=%s",
                payload.get("level"),
                payload.get("instrument_id"),
                target.netloc or "local",
            )
            return
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            if attempt == 1:
                _LOGGER.warning("Dyops webhook failed after retry for %s: %s", url, exc)


async def send_webhooks(payload: dict[str, Any]) -> None:
    """POST an escalation payload to every configured URL, concurrently."""
    urls = configured_urls()
    if not urls:
        return
    try:
        timeout = httpx.Timeout(_TIMEOUT_SECONDS)
        async with httpx.AsyncClient(timeout=timeout) as client:
            await asyncio.gather(
                *(_post_with_retry(client, url, payload) for url in urls),
            )
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("Dyops webhook dispatch failed: %s", exc)
