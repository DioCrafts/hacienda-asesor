from hacienda_gpt.llm.retrieval_profiles import build_decision_profile, build_explain_profile


def test_decision_profile_defaults_and_filters() -> None:
    profile = build_decision_profile(fiscal_year=2025, metadata_filter={"scope": "nacional"})
    assert profile.name == "decision_retriever"
    assert profile.similarity_threshold > 0.8
    assert profile.metadata_filter["fiscal_year"] == 2025
    assert profile.metadata_filter["scope"] == "nacional"


def test_explain_profile_defaults_and_filters() -> None:
    profile = build_explain_profile(fiscal_year=2025)
    assert profile.name == "explain_retriever"
    assert profile.similarity_threshold < 0.8
