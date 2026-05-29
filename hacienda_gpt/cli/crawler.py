import os
from typing import Any

import click
from scrapy.crawler import CrawlerProcess
from scrapy.spiders import Spider

from hacienda_gpt.crawler.crawlers import (
    AgenciaTributariaPDFCrawler,
    AgenciaTributariaWebCrawler,
    BOECCAACrawler,
    CENDOJCrawler,
    RegionalBoletinCrawler,
    TEACCrawler,
)

CRAWLER_MAPPING: dict[str, type[Spider]] = {
    "web": AgenciaTributariaWebCrawler,
    "pdf": AgenciaTributariaPDFCrawler,
    "teac": TEACCrawler,
    "cendoj": CENDOJCrawler,
    "boe-ccaa": BOECCAACrawler,
    "boletin": RegionalBoletinCrawler,
}

_PARAM_FREE_CRAWLERS = {AgenciaTributariaPDFCrawler, AgenciaTributariaWebCrawler}

SETTINGS = {
    "ROBOTSTXT_OBEY": True,
    "HTTPCACHE_ENABLED": True,
    # `scrapy-fake-useragent` 1.4.4 (the version pinned in uv.lock) reaches
    # for `self.EXCEPTIONS_TO_RETRY` on Scrapy's `RetryMiddleware`, but that
    # attribute was removed in Scrapy 2.11+. Hitting it blows up the very
    # first request (typically the robots.txt fetch) and the crawl ends with
    # zero items. We therefore disable the fake-UA retry shim and let the
    # stock Scrapy retry path stay in charge. We still randomize the User
    # Agent per request via `RandomUserAgentMiddleware`, which is unaffected.
    "DOWNLOADER_MIDDLEWARES": {
        "scrapy.downloadermiddlewares.useragent.UserAgentMiddleware": None,
        "scrapy_fake_useragent.middleware.RandomUserAgentMiddleware": 400,
        # Route every HTTPS request through scrapy-playwright so we sidestep
        # the X509/pyOpenSSL incompatibility on the stock Scrapy downloader.
        "hacienda_gpt.crawler.middlewares.ForcePlaywrightMiddleware": 543,
    },
    "DOWNLOAD_HANDLERS": {
        "http": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
        "https": "scrapy_playwright.handler.ScrapyPlaywrightDownloadHandler",
    },
    "TWISTED_REACTOR": "twisted.internet.asyncioreactor.AsyncioSelectorReactor",
    "PLAYWRIGHT_BROWSER_TYPE": "chromium",
    "PLAYWRIGHT_LAUNCH_OPTIONS": {
        "executable_path": os.environ.get("PLAYWRIGHT_BROWSERS_BINARY_PATH"),
    },
}


def start_crawler(
    crawler_class: type[Spider],
    settings: dict[str, Any],
    folder: str,
    mode: str,
    snapshot_date: str | None,
    extra_kwargs: dict[str, Any] | None = None,
) -> None:
    process = CrawlerProcess(settings=settings)
    kwargs = {"folder": os.path.abspath(folder), "snapshot_date": snapshot_date}
    if crawler_class in _PARAM_FREE_CRAWLERS:
        kwargs["mode"] = mode
    if extra_kwargs:
        kwargs.update(extra_kwargs)
    process.crawl(crawler_class, **kwargs)
    process.start(install_signal_handlers=True)


@click.command()
@click.option("--folder", default="./data/html", help="Folder to store files")
@click.option("--depth", default=1, help="Max depth to crawl. 0 for unlimited depth")
@click.option(
    "--crawler",
    type=click.Choice(["web", "pdf", "teac", "cendoj", "boe-ccaa", "boletin"]),
    default="web",
    help="Type of crawler to use",
)
@click.option("--mode", default="flat", help="File storage mode. Can be 'flat' or other modes")
@click.option("--snapshot-date", default=None, help="Snapshot date folder name (YYYY-MM-DD)")
@click.option("--teac-start-id", default=1, type=int, help="DYCTEA criterion start id (only used by --crawler teac)")
@click.option("--teac-end-id", default=100, type=int, help="DYCTEA criterion end id (only used by --crawler teac)")
@click.option("--cendoj-urls-file", default=None, help="Path to a file with one CENDOJ resolution URL per line")
@click.option("--cendoj-urls", default=None, help="Semicolon-separated CENDOJ resolution URLs")
@click.option(
    "--cendoj-only-tributario/--cendoj-keep-all", default=True, help="Keep only resolutions flagged as tributario"
)
@click.option("--ccaa", default=None, help="Comma-separated CCAA keys for --crawler boe-ccaa (default: all)")
@click.option("--skip-unknown-ccaa", is_flag=True, default=False, help="Only crawl CCAA codes verified as published")
@click.option(
    "--boletines", default=None, help="Comma-separated boletin keys for --crawler boletin (default: all bundled)"
)
@click.option("--boletin-terms", default=None, help="Override the per-spec default search terms (comma separated)")
def cli(
    folder: str,
    depth: int,
    crawler: str,
    mode: str,
    snapshot_date: str | None,
    teac_start_id: int,
    teac_end_id: int,
    cendoj_urls_file: str | None,
    cendoj_urls: str | None,
    cendoj_only_tributario: bool,
    ccaa: str | None,
    skip_unknown_ccaa: bool,
    boletines: str | None,
    boletin_terms: str | None,
) -> None:
    settings = {**SETTINGS, **{"DEPTH_LIMIT": depth}}
    crawler_class = CRAWLER_MAPPING[crawler]
    extra: dict[str, Any] = {}
    if crawler == "teac":
        extra = {"start_id": teac_start_id, "end_id": teac_end_id}
    elif crawler == "cendoj":
        extra = {
            "urls": cendoj_urls,
            "urls_file": cendoj_urls_file,
            "only_tributario": cendoj_only_tributario,
        }
    elif crawler == "boe-ccaa":
        extra = {"ccaa": ccaa, "skip_unknown": skip_unknown_ccaa}
    elif crawler == "boletin":
        extra = {"boletines": boletines, "terms": boletin_terms}
    start_crawler(crawler_class, settings, folder, mode, snapshot_date, extra_kwargs=extra)


if __name__ == "__main__":
    cli()
