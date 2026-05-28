"""Scrapy downloader middlewares used by every crawler in this project."""

from __future__ import annotations

from typing import Any


class ForcePlaywrightMiddleware:
    """Route every HTTPS request through scrapy-playwright.

    Why: the venv pins versions of Twisted, pyOpenSSL and `service-identity`
    that no longer agree on the X509 API (``'X509' object has no attribute
    'get_extension'``). Any HTTPS request that goes through Scrapy's stock
    downloader fails at TLS handshake time, killing the PDF, TEAC and BOE
    crawlers before they fetch a single item. scrapy-playwright bypasses
    the broken stack entirely, but it only kicks in when the request carries
    ``meta['playwright'] = True``. The web crawler already sets that flag on
    every yield; the others do not. This middleware backfills the flag for
    every HTTPS request so the same workaround applies project-wide without
    touching every spider.
    """

    def process_request(self, request: Any, spider: Any) -> None:
        if request.url.startswith("https://") and "playwright" not in request.meta:
            request.meta["playwright"] = True
        return None
