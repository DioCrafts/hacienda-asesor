import time
from datetime import UTC, datetime
from uuid import uuid4

import requests
import streamlit as st
from langchain_core.messages import AIMessage, HumanMessage

from hacienda_gpt.decision.interpreter_light import detect_facts_and_missing
from hacienda_gpt.decision.rules_engine import evaluate_rules
from hacienda_gpt.decision.schemas import CaseState
from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore
from hacienda_gpt.llm.chain import create_openai_chain
from hacienda_gpt.settings import API_BASE_URL, DECISION_DEBUG_MODE, DECISION_STATE_DB_PATH, UI_USE_API
from hacienda_gpt.utils import MissingOpenAIAPIKeyError, configure_logging, get_openai_api_key

# Custom image for the app icon and the assistant's avatar
bot_logo = "https://sede.agenciatributaria.gob.es/static_files/Sede/Tema/Agencia_tributaria/Memorias/2018/Imagenes/Introduccion.jpg"


@st.cache_resource
def load_chain():
    try:
        openai_api_key = get_openai_api_key()
    except MissingOpenAIAPIKeyError:
        st.error("Falta OPENAI_API_KEY en el entorno. Configúrala para usar HaciendaGPT.")
        st.stop()
    return create_openai_chain(openai_api_key=openai_api_key)


@st.cache_resource
def load_case_store() -> SQLiteCaseStateStore:
    return SQLiteCaseStateStore(DECISION_STATE_DB_PATH)


def _build_chat_history(messages: list[dict[str, str]]) -> list[HumanMessage | AIMessage]:
    history: list[HumanMessage | AIMessage] = []
    for message in messages:
        if message["role"] == "assistant":
            history.append(AIMessage(content=message["content"]))
        elif message["role"] == "user":
            history.append(HumanMessage(content=message["content"]))
    return history


def _ensure_case_id() -> str:
    if "case_id" not in st.session_state:
        st.session_state["case_id"] = f"case_{uuid4().hex}"
    return st.session_state["case_id"]


def _api_create_case_if_needed() -> str:
    if "api_case_id" in st.session_state:
        return st.session_state["api_case_id"]
    payload = {
        "user_id": st.session_state.get("user_id", "streamlit_user"),
        "jurisdiction": "ES",
        "tax_period": str(datetime.now(UTC).year),
    }
    r = requests.post(f"{API_BASE_URL}/cases", json=payload, timeout=20)
    r.raise_for_status()
    case_id = r.json()["case_id"]
    st.session_state["api_case_id"] = case_id
    return case_id


def _api_process_turn(user_input: str) -> dict:
    case_id = _api_create_case_if_needed()
    r = requests.post(f"{API_BASE_URL}/cases/{case_id}/turn", json={"user_input": user_input}, timeout=30)
    r.raise_for_status()
    return r.json()


def _persist_turn_local(store: SQLiteCaseStateStore, case_id: str, user_input: str, assistant_output: str) -> CaseState:
    now = datetime.now(UTC)
    existing = store.get_case(case_id)
    user_id = st.session_state.get("user_id", "streamlit_user")

    facts, missing_facts = detect_facts_and_missing(user_input)
    if existing is None:
        case_state = CaseState(
            case_id=case_id,
            user_id=user_id,
            jurisdiction="ES",
            tax_period=str(now.year),
            facts=facts,
            missing_facts=missing_facts,
            created_at=now,
            updated_at=now,
        )
    else:
        merged_facts = {fact.fact_id: fact for fact in existing.facts}
        for fact in facts:
            merged_facts[fact.fact_id] = fact
        case_state = existing.model_copy(update={"facts": list(merged_facts.values()), "missing_facts": missing_facts, "updated_at": now})

    rules_result = evaluate_rules(case_state=case_state, recent_facts=facts)
    case_state = case_state.model_copy(update={"obligation_candidates": rules_result.candidate_obligations})
    store.save_case(case_state)
    store.append_audit_event(case_id, {"event_type": "turn_persisted", "user_input": user_input})
    store.append_audit_event(case_id, {"event_type": "assistant_responded", "assistant_output": assistant_output})
    return case_state


def _build_obligation_cards(case_state: CaseState) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    for obligation in case_state.obligation_candidates:
        sources = ", ".join(sorted({e.title for e in obligation.evidence_refs if e.title})) or "Sin fuentes"
        missing = ", ".join(obligation.blocking_missing_facts) or "Ninguno"
        cards.append({"title": obligation.title, "confidence": f"{obligation.confidence:.2f}", "risk": obligation.risk_level.value, "sources": sources, "missing": missing})
    return cards


def _render_obligation_cards(case_state: CaseState) -> None:
    cards = _build_obligation_cards(case_state)
    if not cards:
        return
    st.subheader("Obligaciones candidatas")
    for card in cards:
        with st.container(border=True):
            st.markdown(f"**{card['title']}**")
            col1, col2 = st.columns(2)
            col1.metric("Confianza", card["confidence"])
            col2.markdown(f"**Riesgo:** {card['risk']}")
            st.markdown(f"**Fuentes usadas:** {card['sources']}")
            st.markdown(f"**Dato faltante para confirmar:** {card['missing']}")


def _render_debug(case_state: CaseState) -> None:
    if not DECISION_DEBUG_MODE:
        return
    with st.expander("Decision Debug (CaseState)", expanded=False):
        st.markdown(f"**case_id**: `{case_state.case_id}`")
        st.markdown("**Facts detectados**")
        if case_state.facts:
            st.json([fact.model_dump(mode="json") for fact in case_state.facts])
        else:
            st.info("No se detectaron facts en este turno.")
        st.markdown("**Facts faltantes**")
        if case_state.missing_facts:
            st.json([missing.model_dump(mode="json") for missing in case_state.missing_facts])
        else:
            st.info("No hay facts faltantes detectados en este turno.")


def main():
    configure_logging()
    st.set_page_config(page_title="HaciendaGPT", page_icon=":bank:", layout="centered")
    st.title("HaciendaGPT")

    chain = load_chain()
    store = load_case_store()
    case_id = _ensure_case_id()

    if "messages" not in st.session_state:
        st.session_state["messages"] = [{"role": "assistant", "content": "¡Hola! ¿Cómo puedo ayudarte con tus preguntas relacionadas con la Agencia Tributaria?"}]

    for message in st.session_state.messages:
        with st.chat_message(message["role"], avatar=bot_logo if message["role"] == "assistant" else None):
            st.markdown(message["content"])

    if query := st.chat_input("Preguntáme lo que quieras"):
        st.session_state.messages.append({"role": "user", "content": query})
        with st.chat_message("user"):
            st.markdown(query)

        with st.chat_message("assistant", avatar=bot_logo):
            message_placeholder = st.empty()
            history = _build_chat_history(st.session_state.messages[:-1])
            result = chain.invoke({"input": query, "chat_history": history})
            response = result["answer"]
            full_response = ""
            for chunk in response.split():
                full_response += chunk + " "
                time.sleep(0.05)
                message_placeholder.markdown(full_response + "▌")
            message_placeholder.markdown(full_response)

        st.session_state.messages.append({"role": "assistant", "content": response})

        if UI_USE_API:
            try:
                turn = _api_process_turn(query)
                st.caption(f"API case_id: {turn['case_id']}")
            except Exception as exc:
                st.error(f"Error llamando API backend: {exc}. Usando fallback local.")
                case_state = _persist_turn_local(store=store, case_id=case_id, user_input=query, assistant_output=response)
                _render_debug(case_state)
                _render_obligation_cards(case_state)
        else:
            case_state = _persist_turn_local(store=store, case_id=case_id, user_input=query, assistant_output=response)
            _render_debug(case_state)
            _render_obligation_cards(case_state)


if __name__ == "__main__":
    main()
