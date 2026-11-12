from hacienda_gpt.agent.evidence import EvidenceLedger
from hacienda_gpt.agent.providers.base import ProviderError
from hacienda_gpt.agent.verifier import ResponseType, Verdict, Verifier
from .fakes import ScriptedProvider, corpus


def _fragments():
    ledger = EvidenceLedger()
    return [ledger.register(text=doc.page_content, metadata=doc.metadata) for doc in corpus()[:2]]


def test_verifier_sends_question_answer_and_fragments_and_parses_report() -> None:
    provider = ScriptedProvider(
        [],
        [
            {
                "tipo_respuesta": "respuesta",
                "afirmaciones": [
                    {"texto": "Plazo de dos años", "fragmentos": ["[f2]"], "veredicto": "respaldada", "motivo": ""},
                    {"texto": "Tipo del 19 %", "fragmentos": [], "veredicto": "respaldada", "motivo": ""},
                    {"texto": "Otra", "fragmentos": ["F1"], "veredicto": "inventado", "motivo": "?"},
                    {"texto": "  ", "fragmentos": ["F1"], "veredicto": "respaldada", "motivo": ""},
                    "basura",
                ],
            }
        ],
    )

    report = Verifier(provider).verify(question="¿Plazo?", answer="Dos años [F2].", fragments=_fragments())

    request = provider.json_requests[0]
    assert "<pregunta>\n¿Plazo?" in request["user"] and "Dos años [F2]." in request["user"]
    assert "[F1]" in request["user"] and "vinculante=sí" in request["user"]
    assert request["schema"]["required"] == ["tipo_respuesta", "afirmaciones"]

    assert report.error is None and report.response_type is ResponseType.ANSWER
    assert [(c.text, c.fragment_ids, c.verdict) for c in report.claims] == [
        ("Plazo de dos años", ["F2"], Verdict.SUPPORTED),
        # "Supported" with no cited fragment cannot stand.
        ("Tipo del 19 %", [], Verdict.UNSUPPORTED),
        # An unknown verdict is treated as the conservative one.
        ("Otra", ["F1"], Verdict.UNSUPPORTED),
    ]
    assert len(report.supported) == 1 and len(report.problems) == 2


def test_verifier_provider_failure_is_reported_not_raised() -> None:
    provider = ScriptedProvider([], [ProviderError("timeout")])
    report = Verifier(provider).verify(question="q", answer="a", fragments=[])
    assert report.error == "timeout" and report.claims == []
    assert "no cita ningún fragmento" in provider.json_requests[0]["user"]
