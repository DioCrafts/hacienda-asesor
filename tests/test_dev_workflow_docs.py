from pathlib import Path


def test_makefile_has_quality_targets() -> None:
    makefile = Path("Makefile")
    assert makefile.exists()
    text = makefile.read_text(encoding="utf-8")
    for target in ["format:", "lint:", "type-check:", "test:", "quality:"]:
        assert target in text


def test_dev_workflow_doc_exists_and_mentions_quality_command() -> None:
    workflow = Path("docs/engineering/dev-workflow.md")
    assert workflow.exists()
    text = workflow.read_text(encoding="utf-8")
    assert "make quality" in text
    assert "mypy" in text
    assert "ruff" in text
    assert "pytest" in text
