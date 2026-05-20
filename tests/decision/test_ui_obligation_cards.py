from datetime import UTC, datetime

from hacienda_gpt.decision.schemas import CaseState, EvidenceRef, EvidenceSourceType, ObligationCandidate, RiskLevel
from hacienda_gpt.ui.app import _build_obligation_cards


def test_build_obligation_cards_includes_confidence_sources_and_missing() -> None:
    now = datetime.now(UTC)
    case = CaseState(
        case_id="c1",
        user_id="u1",
        jurisdiction="ES",
        tax_period="2025",
        obligation_candidates=[
            ObligationCandidate(
                obligation_id="obl1",
                title="Posible IRPF",
                description="desc",
                jurisdiction="ES",
                tax_period="2025",
                risk_level=RiskLevel.HIGH,
                confidence=0.87,
                trigger_facts=["residencia_fiscal"],
                blocking_missing_facts=["tipo_renta"],
                evidence_refs=[
                    EvidenceRef(
                        evidence_id="ev1",
                        source_type=EvidenceSourceType.RETRIEVED_DOCUMENT,
                        title="Norma IRPF",
                        locator="https://example.org",
                        confidence=0.9,
                    )
                ],
                created_at=now,
                updated_at=now,
            )
        ],
        created_at=now,
        updated_at=now,
    )

    cards = _build_obligation_cards(case)
    assert len(cards) == 1
    card = cards[0]
    assert card["title"] == "Posible IRPF"
    assert card["confidence"] == "0.87"
    assert "Norma IRPF" in card["sources"]
    assert card["missing"] == "tipo_renta"
