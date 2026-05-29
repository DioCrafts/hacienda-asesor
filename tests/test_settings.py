import importlib

import pytest

import hacienda_gpt.settings as settings


def test_env_bool_truthy_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FEATURE_FLAG", "TrUe")
    assert settings._env_bool("FEATURE_FLAG") is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "random"])
def test_env_bool_falsy_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("FEATURE_FLAG", value)
    assert settings._env_bool("FEATURE_FLAG") is False


def test_env_bool_default_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FEATURE_FLAG", raising=False)
    assert settings._env_bool("FEATURE_FLAG", default=True) is True
    assert settings._env_bool("FEATURE_FLAG", default=False) is False


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ["OPENAI_MODEL", "OPENAI_TEMPERATURE", "FAISS_INDEX_PATH", "FAISS_TRUSTED_INDEX", "TOP_K"]:
        monkeypatch.delenv(key, raising=False)

    reloaded = importlib.reload(settings)

    assert reloaded.OPENAI_MODEL == "gpt-4o-mini"
    assert reloaded.OPENAI_TEMPERATURE == 0.0
    assert reloaded.FAISS_INDEX_PATH == ".faiss"
    assert reloaded.FAISS_TRUSTED_INDEX is False
    assert reloaded.TOP_K == 3


def test_retrieval_threshold_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ["RETRIEVAL_DECISION_THRESHOLD", "RETRIEVAL_EXPLAIN_THRESHOLD"]:
        monkeypatch.delenv(key, raising=False)

    reloaded = importlib.reload(settings)

    assert reloaded.RETRIEVAL_DECISION_THRESHOLD == 0.45
    assert reloaded.RETRIEVAL_EXPLAIN_THRESHOLD == 0.35
    # decision favours precision; explain favours recall.
    assert reloaded.RETRIEVAL_DECISION_THRESHOLD > reloaded.RETRIEVAL_EXPLAIN_THRESHOLD


def test_retrieval_threshold_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RETRIEVAL_DECISION_THRESHOLD", "0.7")
    monkeypatch.setenv("RETRIEVAL_EXPLAIN_THRESHOLD", "0.6")
    try:
        reloaded = importlib.reload(settings)
        assert reloaded.RETRIEVAL_DECISION_THRESHOLD == 0.7
        assert reloaded.RETRIEVAL_EXPLAIN_THRESHOLD == 0.6
    finally:
        # Restore module-level defaults so the mutated singleton does not leak
        # into other test modules that import `settings`.
        monkeypatch.delenv("RETRIEVAL_DECISION_THRESHOLD", raising=False)
        monkeypatch.delenv("RETRIEVAL_EXPLAIN_THRESHOLD", raising=False)
        importlib.reload(settings)
