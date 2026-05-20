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
