import logging
import os


class MissingOpenAIAPIKeyError(RuntimeError):
    pass


def configure_logging():
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(filename)s:%(lineno)s - %(message)s", level=logging.INFO
    )


def get_openai_api_key():
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise MissingOpenAIAPIKeyError("OPENAI_API_KEY missing")
    return api_key
