import logging
import os

import click

from hacienda_gpt.processor.document_loader import build_index
from hacienda_gpt.utils import configure_logging

PRJ_DATA_DIR = os.environ.get("PRJ_DATA_DIR", os.path.expanduser("~"))
FAISS_DIR = os.path.join(PRJ_DATA_DIR, "faiss")
CONTENT_DIR = os.path.join(PRJ_DATA_DIR, "html")


@click.command()
@click.option("--content-dir", type=click.Path(exists=True, file_okay=False, dir_okay=True), default=CONTENT_DIR)
@click.option("--output-dir", type=click.Path(exists=False, file_okay=False, dir_okay=True), default=FAISS_DIR)
@click.option(
    "--max-tokens",
    type=click.IntRange(min=1),
    default=None,
    help="Cap per-chunk tokens for Docling's HybridChunker (default: the embedder's token limit).",
)
@click.option(
    "--num-workers",
    type=click.IntRange(min=1),
    default=11,
    help=(
        "Parallelise Docling parsing across N processes. The embedder is still loaded "
        "once in the parent (no per-worker GPU duplication). Default 11, tuned for "
        "Mac M-series with 10 performance + 4 efficiency cores: matches all 10 perf "
        "cores plus one extra to keep the pool saturated when one worker is stuck on "
        "a giant BOE consolidado. Override down to 1 for sequential debugging or up "
        "for boxes with more cores."
    ),
)
@click.option(
    "--incremental/--full",
    "incremental",
    default=True,
    help=(
        "Reuse work from a previous index when a manifest is present and the "
        "pipeline (embedder + chunker + Docling version) matches. Files whose "
        "SHA-256 has not changed are skipped entirely (no Docling, no embedding). "
        "Use --full to force a fresh build, ignoring any existing manifest."
    ),
)
@click.option(
    "--overwrite-output",
    is_flag=True,
    help=(
        "Allow building into a non-empty output dir. Only required for --full "
        "rebuilds; --incremental runs reuse the existing files by design."
    ),
)
@click.option(
    "--batch-size",
    type=click.IntRange(min=1),
    default=200,
    help=(
        "Number of files per persistence batch. After every batch, FAISS and "
        "manifest are written atomically to disk, so a crash loses at most "
        "the current batch — re-running with --incremental resumes from the "
        "manifest. Smaller = finer recovery granularity, more I/O. 200 ~95s "
        "of work-at-risk on M-series hardware."
    ),
)
def cli(content_dir, output_dir, max_tokens, num_workers, incremental, overwrite_output, batch_size):
    configure_logging()

    # Guarding logic differs by mode:
    # * full rebuild over a non-empty dir without --overwrite-output: refuse,
    #   we'd silently clobber the previous index.
    # * incremental run: we are *expected* to find an existing index there, so
    #   --overwrite-output is irrelevant (in fact, requiring it would surprise
    #   the caller every time).
    output_exists = os.path.exists(output_dir) and os.listdir(output_dir)
    if not incremental and output_exists and not overwrite_output:
        logging.error(
            f"Directory {output_dir} exists and contains files. Pass --overwrite-output to rebuild from scratch."
        )
        return

    args = {
        "content_dir": content_dir,
        "output_dir": output_dir,
        "max_tokens": max_tokens,
        "num_workers": num_workers,
        "incremental": incremental,
        "batch_size": batch_size,
    }

    build_index(args)

    logging.info("Local FAISS index has been successfully saved")


if __name__ == "__main__":
    cli()
