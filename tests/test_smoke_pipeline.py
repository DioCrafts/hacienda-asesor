from datetime import UTC, datetime

from click.testing import CliRunner

from hacienda_gpt.cli import crawler as crawler_cli
from hacienda_gpt.cli import processor as processor_cli
from hacienda_gpt.llm import chain


def test_smoke_pipeline_crawler_to_index_to_question(monkeypatch) -> None:
    calls: dict[str, object] = {}

    def fake_start_crawler(crawler_class, settings, folder, mode, snapshot_date):
        calls["crawler"] = {
            "crawler_class": crawler_class.__name__,
            "depth": settings["DEPTH_LIMIT"],
            "folder": folder,
            "mode": mode,
            "snapshot_date": snapshot_date,
        }

    def fake_process_with_gpt4all(args):
        calls["processor"] = args

    class DummyChain:
        def invoke(self, payload):
            calls["invoke"] = payload
            return {"answer": "Calendario del contribuyente: https://sede.agenciatributaria.gob.es"}

    monkeypatch.setattr(crawler_cli, "start_crawler", fake_start_crawler)
    monkeypatch.setattr(processor_cli, "process_with_gpt4all", fake_process_with_gpt4all)
    monkeypatch.setattr(chain, "create_openai_chain", lambda openai_api_key: DummyChain())

    runner = CliRunner()
    snapshot = datetime.now(UTC).strftime("%Y-%m-%d")

    crawl_result = runner.invoke(
        crawler_cli.cli,
        ["--crawler", "web", "--folder", "./tmp/html", "--depth", "1", "--mode", "flat", "--snapshot-date", snapshot],
    )
    assert crawl_result.exit_code == 0

    process_result = runner.invoke(
        processor_cli.cli,
        [
            "--content-dir",
            ".",
            "--output-dir",
            "./tmp/faiss",
            "--chunk-size",
            "500",
            "--chunk-overlap",
            "50",
            "--embedder",
            "gpt4all",
            "--overwrite-output",
        ],
    )
    assert process_result.exit_code == 0

    rag_chain = chain.create_openai_chain(openai_api_key="test-key")
    answer = rag_chain.invoke({"input": "¿Dónde veo el calendario?", "chat_history": []})["answer"]

    assert "calendario" in answer.lower()
    assert calls["crawler"]
    assert calls["processor"]
    assert calls["invoke"]["input"] == "¿Dónde veo el calendario?"
