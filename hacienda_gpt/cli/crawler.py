import os
from typing import Any

import click
from scrapy.crawler import CrawlerProcess
from scrapy.spiders import Spider

from hacienda_gpt.crawler.crawlers import (
    AgenciaTributariaPDFCrawler,
    AgenciaTributariaWebCrawler,
)

CRAWLER_MAPPING: dict[str, type[Spider]] = {"web": AgenciaTributariaWebCrawler, "pdf": AgenciaTributariaPDFCrawler}

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
    crawler_class: type[Spider], settings: dict[str, Any], folder: str, mode: str, snapshot_date: str | None
) -> None:
    process = CrawlerProcess(settings=settings)
    process.crawl(crawler_class, folder=os.path.abspath(folder), mode=mode, snapshot_date=snapshot_date)
    process.start(install_signal_handlers=True)


@click.command()
@click.option("--folder", default="./data/html", help="Folder to store files")
@click.option("--depth", default=1, help="Max depth to crawl. 0 for unlimited depth")
@click.option("--crawler", type=click.Choice(["web", "pdf"]), default="web", help="Type of crawler to use")
@click.option("--mode", default="flat", help="File storage mode. Can be 'flat' or other modes")
@click.option("--snapshot-date", default=None, help="Snapshot date folder name (YYYY-MM-DD)")
def cli(folder: str, depth: int, crawler: str, mode: str, snapshot_date: str | None) -> None:
    settings = {**SETTINGS, **{"DEPTH_LIMIT": depth}}
    crawler_class = CRAWLER_MAPPING[crawler]
    start_crawler(crawler_class, settings, folder, mode, snapshot_date)


if __name__ == "__main__":
    cli()
