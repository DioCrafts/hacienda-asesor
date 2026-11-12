"""Scripted doubles for the agent tests: no network, no models, no FAISS."""

from __future__ import annotations

from typing import Any

from langchain_core.documents import Document

from hacienda_gpt.agent.providers.base import ModelTurn, ToolCall, ToolResult, ToolSpec
from hacienda_gpt.agent.search import SearchFilters, build_filter_predicate


def tool_turn(*calls: tuple[str, dict[str, Any]]) -> ModelTurn:
    return ModelTurn(
        text="",
        tool_calls=[ToolCall(id=f"call_{i}", name=name, arguments=args) for i, (name, args) in enumerate(calls)],
        stop_reason="tool_use",
        usage={"input_tokens": 10, "output_tokens": 5},
    )


def text_turn(text: str) -> ModelTurn:
    return ModelTurn(text=text, tool_calls=[], stop_reason="end", usage={"input_tokens": 10, "output_tokens": 5})


class ScriptedConversation:
    def __init__(self, provider: ScriptedProvider) -> None:
        self._provider = provider

    def step(self, *, allow_tools: bool = True) -> ModelTurn:
        self._provider.allow_tools_log.append(allow_tools)
        return self._provider.turns.pop(0)

    def add_tool_results(self, results: list[ToolResult]) -> None:
        self._provider.tool_results.extend(results)

    def add_user_message(self, text: str) -> None:
        self._provider.user_messages.append(text)


class ScriptedProvider:
    """Replays a fixed list of turns and JSON completions, recording all inputs."""

    name = "scripted"
    model = "scripted-1"

    def __init__(self, turns: list[ModelTurn], json_responses: list[dict[str, Any] | Exception] | None = None) -> None:
        self.turns = list(turns)
        self.json_responses = list(json_responses or [])
        self.started: dict[str, Any] = {}
        self.tool_results: list[ToolResult] = []
        self.user_messages: list[str] = []
        self.allow_tools_log: list[bool] = []
        self.json_requests: list[dict[str, Any]] = []

    def start(
        self, *, system: str, tools: list[ToolSpec], history: list[dict[str, str]], user_message: str
    ) -> ScriptedConversation:
        self.started = {"system": system, "tools": tools, "history": history, "user_message": user_message}
        return ScriptedConversation(self)

    def complete_json(self, *, system: str, user: str, schema: dict[str, Any], schema_name: str) -> dict[str, Any]:
        self.json_requests.append({"system": system, "user": user, "schema": schema, "schema_name": schema_name})
        response = self.json_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeSearcher:
    """Returns the documents whose metadata passes the filters, ignoring the query."""

    def __init__(self, docs: list[Document]) -> None:
        self.docs = docs
        self.calls: list[tuple[str, int, SearchFilters]] = []

    def search(self, query: str, *, k: int, filters: SearchFilters) -> list[Document]:
        self.calls.append((query, k, filters))
        predicate = build_filter_predicate(filters)
        return [doc for doc in self.docs if predicate(doc.metadata)][:k]


def corpus() -> list[Document]:
    return [
        Document(
            page_content="Podrán excluirse de gravamen las ganancias patrimoniales obtenidas por la transmisión de "
            "la vivienda habitual, siempre que el importe total se reinvierta en una nueva vivienda habitual.",
            metadata={
                "title": "Ley 35/2006 IRPF",
                "section": "Artículo 38",
                "source_url": "https://www.boe.es/buscar/act.php?id=BOE-A-2006-20764",
                "source_authority": "BOE",
                "chunk_id": "boe-irpf-38",
            },
        ),
        Document(
            page_content="La reinversión deberá efectuarse en un período no superior a dos años desde la fecha de "
            "transmisión de la vivienda habitual.",
            metadata={
                "title": "Consulta V0058-25",
                "source_url": "data/html/2026-01-01/dgt-vinculantes/2025/V0058-25.html",
                "vinculante": True,
                "fecha_resolucion": "2025-01-20",
                "num_consulta": "V0058-25",
                "chunk_id": "dgt-v0058-25-3",
            },
        ),
        Document(
            page_content="Deducción autonómica por alquiler de vivienda habitual para jóvenes.",
            metadata={
                "title": "Código de Andalucía",
                "source_url": "https://www.boe.es/biblioteca_juridica/codigos/codigo.php?id=229",
                "source_authority": "BOE",
                "jurisdiction_scope": "autonomico:01",
                "covered_tributos": ["IRPF_autonomico", "ISD"],
                "chunk_id": "boe-ccaa-01-9",
            },
        ),
    ]


def verdict(response_type: str = "respuesta", *claims: tuple[str, list[str], str]) -> dict[str, Any]:
    return {
        "tipo_respuesta": response_type,
        "afirmaciones": [
            {"texto": text, "fragmentos": ids, "veredicto": v, "motivo": "" if v == "respaldada" else "no lo dice"}
            for text, ids, v in claims
        ],
    }
