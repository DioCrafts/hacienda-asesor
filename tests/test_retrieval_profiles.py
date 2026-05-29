from hacienda_gpt.llm.retrieval_profiles import build_decision_profile, build_explain_profile
import hacienda_gpt.settings as settings


def test_decision_profile_defaults_and_filters() -> None:
    profile = build_decision_profile(fiscal_year=2025, metadata_filter={"scope": "nacional"})
    assert profile.name == "decision_retriever"
    assert profile.similarity_threshold == settings.RETRIEVAL_DECISION_THRESHOLD
    assert profile.metadata_filter["fiscal_year"] == 2025
    assert profile.metadata_filter["scope"] == "nacional"


def test_explain_profile_defaults_and_filters() -> None:
    profile = build_explain_profile(fiscal_year=2025)
    assert profile.name == "explain_retriever"
    assert profile.similarity_threshold == settings.RETRIEVAL_EXPLAIN_THRESHOLD


def test_decision_profile_is_stricter_than_explain() -> None:
    # decision favours precision; explain favours recall.
    assert build_decision_profile().similarity_threshold > build_explain_profile().similarity_threshold


def test_profiles_read_thresholds_from_settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "RETRIEVAL_DECISION_THRESHOLD", 0.61)
    monkeypatch.setattr(settings, "RETRIEVAL_EXPLAIN_THRESHOLD", 0.42)
    assert build_decision_profile().similarity_threshold == 0.61
    assert build_explain_profile().similarity_threshold == 0.42
