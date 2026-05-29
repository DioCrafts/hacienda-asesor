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
    default=1,
    help=(
        "Parallelise Docling parsing across N processes. The embedder is still loaded "
        "once in the parent (no per-worker GPU duplication). Recommended: number of "
        "physical CPU cores. Default 1 (sequential)."
    ),
)
@click.option("--overwrite-output", is_flag=True)
def cli(content_dir, output_dir, max_tokens, num_workers, overwrite_output):
    configure_logging()

    if not overwrite_output and os.path.exists(output_dir) and os.listdir(output_dir):
        logging.error(f"Directory {output_dir} exists and contains files. Aborting.")
        return

    args = {
        "content_dir": content_dir,
        "output_dir": output_dir,
        "max_tokens": max_tokens,
        "num_workers": num_workers,
    }

    build_index(args)

    logging.info("Local FAISS index has been successfully saved")


if __name__ == "__main__":
    cli()
