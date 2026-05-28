import os
from typing import Any

import click
from scrapy.crawler import CrawlerProcess
from scrapy.spiders import Spider

from hacienda_gpt.crawler.crawlers import (
    AgenciaTributariaPDFCrawler,
    AgenciaTributariaWebCrawler,
    TEACCrawler,
)

CRAWLER_MAPPING: dict[str, type[Spider]] = {
    "web": AgenciaTributariaWebCrawler,
    "pdf": AgenciaTributariaPDFCrawler,
    "teac": TEACCrawler,
}

SETTINGS = {
    "ROBOTSTXT_OBEY": True,
    "HTTPCACHE_ENABLED": True,
    "DOWNLOADER_MIDDLEWARES": {
        "scrapy.downloadermiddlewares.useragent.UserAgentMiddleware": None,
        "scrapy.downloadermiddlewares.retry.RetryMiddleware": None,
        "scrapy_fake_useragent.middleware.RandomUserAgentMiddleware": 400,
        "scrapy_fake_useragent.middleware.RetryUserAgentMiddleware": 401,
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
    if crawler_class is not TEACCrawler:
        kwargs["mode"] = mode
    if extra_kwargs:
        kwargs.update(extra_kwargs)
    process.crawl(crawler_class, **kwargs)
    process.start(install_signal_handlers=True)


@click.command()
@click.option("--folder", default="./data/html", help="Folder to store files")
@click.option("--depth", default=1, help="Max depth to crawl. 0 for unlimited depth")
@click.option("--crawler", type=click.Choice(["web", "pdf", "teac"]), default="web", help="Type of crawler to use")
@click.option("--mode", default="flat", help="File storage mode. Can be 'flat' or other modes")
@click.option("--snapshot-date", default=None, help="Snapshot date folder name (YYYY-MM-DD)")
@click.option("--teac-start-id", default=1, type=int, help="DYCTEA criterion start id (only used by --crawler teac)")
@click.option("--teac-end-id", default=100, type=int, help="DYCTEA criterion end id (only used by --crawler teac)")
def cli(
    folder: str,
    depth: int,
    crawler: str,
    mode: str,
    snapshot_date: str | None,
    teac_start_id: int,
    teac_end_id: int,
) -> None:
    settings = {**SETTINGS, **{"DEPTH_LIMIT": depth}}
    crawler_class = CRAWLER_MAPPING[crawler]
    extra: dict[str, Any] = {}
    if crawler == "teac":
        extra = {"start_id": teac_start_id, "end_id": teac_end_id}
    start_crawler(crawler_class, settings, folder, mode, snapshot_date, extra_kwargs=extra)


if __name__ == "__main__":
    cli()
