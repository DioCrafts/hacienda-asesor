"""Streamlit front-end for HaciendaGPT.

Presentation only. The RAG chain, the grounding gate, the decision
engine and the API client are untouched — every refactor in this file
maps backend-provided fields (the ``AnswerEnvelope``, the
``CaseState.obligation_candidates``) onto the bespoke components defined
in ``hacienda_gpt/ui/assets/style.css``:

* trust-strip  — colour-coded grounding verdict pinned above every reply.
* source cards — one card per citation, always visible (no expander).
* obligation cards — risk-coloured tile with a confidence bar and the
  missing fact framed as a question.
* chips        — sidebar topic scope + suggested-question shortcuts.

Identity tokens live in ``hacienda_gpt/ui/assets/style.css`` and
``.streamlit/config.toml``. The brand monogram is served locally from
``hacienda_gpt/ui/assets/logo.svg`` — no hot-link to any tax-authority
asset.
"""
from __future__ import annotations

import base64
from datetime import UTC, datetime
import html
from pathlib import Path
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
import requests
import streamlit as st

from hacienda_gpt.decision.fact_extractor import FactExtractor, default_fact_extractor
from hacienda_gpt.decision.schemas import CaseState, ObligationCandidate, RiskLevel
from hacienda_gpt.decision.state_store_sqlite import SQLiteCaseStateStore
from hacienda_gpt.decision.turn_service import TurnOutcome, process_turn
from hacienda_gpt.llm.chain import answer_with_grounding, create_openai_chain
from hacienda_gpt.llm.grounding import AnswerEnvelope, AnswerMode, Citation
from hacienda_gpt.settings import API_BASE_URL, DECISION_DEBUG_MODE, DECISION_STATE_DB_PATH, UI_USE_API
from hacienda_gpt.utils import MissingOpenAIAPIKeyError, configure_logging, get_openai_api_key

_ASSETS = Path(__file__).parent / "assets"

# Hard-coded illustrative chips. The suggested-question copy lives next
# to the UI rather than in settings so changing wording is a one-line PR
# that does not require an env restart.
_TOPICS_SUPPORTED = ("IRPF", "IVA", "Autónomos", "Modelo 720", "ISD")
_SUGGESTED_QUESTIONS = (
    "¿Qué modelos presento como autónomo?",
    "Plazos de la Renta 2024",
    "¿Debo declarar criptomonedas?",
    "¿Tributa una herencia entre padre e hijo?",
)
_DISCLAIMER_FULL = (
    "<strong>Herramienta de apoyo.</strong> No sustituye asesoramiento "
    "fiscal profesional ni criterio oficial. HaciendaGPT puede equivocarse "
    "y se abstiene si no encuentra base normativa. Verifica siempre con un "
    "profesional."
)


# --------------------------------------------------------------------- #
# Cached resources (chain + storage). Behaviour-side, untouched.
# --------------------------------------------------------------------- #
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


@st.cache_resource
def load_fact_extractor() -> FactExtractor:
    # Same extractor the API uses (OpenAI structured output when a key is set,
    # regex fallback otherwise), so UI and API interpret turns identically.
    return default_fact_extractor()


@st.cache_resource
def _load_css() -> str:
    return (_ASSETS / "style.css").read_text(encoding="utf-8")


@st.cache_resource
def _load_logo_svg() -> str:
    return (_ASSETS / "logo.svg").read_text(encoding="utf-8")


@st.cache_resource
def _load_logo_data_uri() -> str:
    """Encode the monogram as a base64 data URI so we can pass it as the
    chat-message avatar. ``st.chat_message`` accepts a URL string only;
    serving the file from disk requires a data URI to keep everything
    self-contained without a static-files server."""
    svg = (_ASSETS / "logo.svg").read_bytes()
    return "data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii")


def _inject_theme() -> None:
    """Inject the CSS bundle once per session."""
    st.markdown(f"<style>{_load_css()}</style>", unsafe_allow_html=True)


# --------------------------------------------------------------------- #
# Chat-history adapters and persistence helpers. Untouched.
# --------------------------------------------------------------------- #
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


def _api_process_turn(user_input: str, chat_history: list[dict[str, str]] | None = None) -> dict:
    case_id = _api_create_case_if_needed()
    r = requests.post(
        f"{API_BASE_URL}/cases/{case_id}/turn",
        json={"user_input": user_input, "chat_history": chat_history or []},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _persist_turn_local(
    store: SQLiteCaseStateStore,
    case_id: str,
    user_input: str,
    assistant_output: str,
    extractor: FactExtractor | None = None,
    chat_history: list[dict[str, str]] | None = None,
) -> TurnOutcome:
    now = datetime.now(UTC)
    existing = store.get_case(case_id)
    user_id = st.session_state.get("user_id", "streamlit_user")

    case = existing or CaseState(
        case_id=case_id,
        user_id=user_id,
        jurisdiction="ES",
        tax_period=str(now.year),
        created_at=now,
        updated_at=now,
    )

    outcome = process_turn(
        case=case,
        user_input=user_input,
        extractor=extractor or default_fact_extractor(),
        chat_history=chat_history,
    )
    store.save_case(outcome.case_state)
    store.append_audit_event(case_id, {"event_type": "turn_persisted", "user_input": user_input})
    store.append_audit_event(case_id, {"event_type": "assistant_responded", "assistant_output": assistant_output})
    return outcome


# --------------------------------------------------------------------- #
# Render: trust strip + source cards (Mejora 02 — confianza al frente).
# --------------------------------------------------------------------- #
_TRUST_STRIP_COPY = {
    AnswerMode.CITED: (
        "cited",
        "Respuesta con citas normativas",
        "Cada afirmación se apoya en las fuentes listadas debajo.",
    ),
    AnswerMode.UNCITED: (
        "uncited",
        "Orientativo · sin citas normativas verificadas",
        "Trata esta respuesta como orientación inicial y verifícala.",
    ),
    AnswerMode.ABSTAINED: (
        "abstain",
        "Abstención · no encontramos base normativa suficiente",
        "Prefiero no responder antes que arriesgar un dato sin fuente.",
    ),
}


def _render_trust_strip(envelope: AnswerEnvelope) -> None:
    """Always-visible badge that names the grounding verdict in a single
    line. Replaces the stock ``st.success/warning/info`` row so the same
    visual language can be used everywhere (chat reply + sidebar)."""
    css_cls, label, default_reason = _TRUST_STRIP_COPY[envelope.mode]
    reason = envelope.reason or default_reason
    st.markdown(
        f'<div class="trust-strip {css_cls}">'
        f'<span class="label">{html.escape(label)}</span>'
        f'<span class="reason">— {html.escape(reason)}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )


def _render_source_cards(envelope: AnswerEnvelope) -> None:
    """Per-citation card, rendered as raw HTML so the layout is identical
    whether the citation came from the local chain or the API. Cards are
    ALWAYS visible — no expander — because the brief is explicit: trust
    must not be one click away."""
    if not envelope.citations:
        return
    count = len(envelope.citations)
    heading = f"Fuentes citadas ({count})"
    cards_html = "".join(_format_source_card(c) for c in envelope.citations)
    st.markdown(
        f'<div class="sources-block">'
        f'<div class="sources-heading">{html.escape(heading)}</div>'
        f'{cards_html}'
        f'</div>',
        unsafe_allow_html=True,
    )


def _format_source_card(c: Citation) -> str:
    title = html.escape(c.title or "Fuente sin título")
    section = (
        f'<span class="section">— {html.escape(c.section)}</span>' if c.section else ""
    )
    locator = html.escape(c.locator or "")
    locator_html = (
        f'<div><span class="locator">{locator}</span></div>' if locator else ""
    )
    snippet_html = (
        f'<div class="snippet">«{html.escape(c.snippet.strip())}»</div>'
        if c.snippet
        else ""
    )
    link_html = ""
    if c.locator and c.locator.startswith(("http://", "https://")):
        link_html = (
            f'<a class="link" href="{html.escape(c.locator)}" target="_blank" rel="noopener">'
            f'Abrir fuente original ↗</a>'
        )
    return (
        f'<div class="source-card">'
        f'<div class="title">{title}{section}</div>'
        f'{locator_html}'
        f'{snippet_html}'
        f'{link_html}'
        f'</div>'
    )


# --------------------------------------------------------------------- #
# Render: obligation cards (Mejora 05 — riesgo por color + confianza).
# --------------------------------------------------------------------- #
_RISK_CSS = {
    RiskLevel.LOW: ("low", "Riesgo bajo"),
    RiskLevel.MEDIUM: ("medium", "Riesgo medio"),
    RiskLevel.HIGH: ("high", "Riesgo alto"),
    RiskLevel.CRITICAL: ("high", "Riesgo crítico"),
}


def _format_missing_as_question(missing: list[str]) -> str:
    if not missing:
        return ""
    if len(missing) == 1:
        return f"¿Puedes confirmarme <em>{html.escape(missing[0])}</em>?"
    items = "</li><li>".join(html.escape(m) for m in missing)
    return f"Aún me falta saber: <ul><li>{items}</li></ul>"


def _build_obligation_cards(case_state: CaseState) -> list[dict[str, str]]:
    """Build presentation-ready data for each candidate obligation.

    Separated from the HTML so the field mapping (confidence formatting,
    source aggregation, missing-fact summary) can be unit-tested without a
    Streamlit runtime.
    """
    cards: list[dict[str, str]] = []
    for o in case_state.obligation_candidates:
        css_cls, risk_label = _RISK_CSS.get(o.risk_level, ("medium", o.risk_level.value))
        sources = ", ".join(
            sorted({e.title for e in o.evidence_refs if getattr(e, "title", None)})
        ) or "Sin fuentes asociadas"
        cards.append(
            {
                "title": o.title,
                "risk_label": risk_label,
                "risk_css": css_cls,
                "confidence": f"{o.confidence:.2f}",
                "pct": str(int(round(o.confidence * 100))),
                "sources": sources,
                "missing": ", ".join(o.blocking_missing_facts),
                "missing_question": _format_missing_as_question(o.blocking_missing_facts),
            }
        )
    return cards


def _format_obligation_card(card: dict[str, str]) -> str:
    missing_block = (
        f'<div class="label">Dato que falta para confirmar</div>'
        f'<div class="missing-q">{card["missing_question"]}</div>'
        if card["missing_question"]
        else ""
    )
    return (
        f'<div class="obligation-card">'
        f'<div class="heading">'
        f'<span class="title">{html.escape(card["title"])}</span>'
        f'<span class="risk-badge {card["risk_css"]}">{html.escape(card["risk_label"])}</span>'
        f'</div>'
        f'<div class="confidence-row">'
        f'<div class="confidence-bar"><div class="fill" style="width:{card["pct"]}%"></div></div>'
        f'<span class="pct">{card["pct"]}%</span>'
        f'</div>'
        f'<div class="label">Base normativa</div>'
        f'<div class="sources-line">{html.escape(card["sources"])}</div>'
        f'{missing_block}'
        f'</div>'
    )


def _render_obligation_cards(case_state: CaseState) -> None:
    cards = _build_obligation_cards(case_state)
    if not cards:
        return
    st.subheader("Obligaciones candidatas")
    cards_html = "".join(_format_obligation_card(c) for c in cards)
    st.markdown(cards_html, unsafe_allow_html=True)


# --------------------------------------------------------------------- #
# Render: follow-ups + decision debug (kept compatible with the brief).
# --------------------------------------------------------------------- #
def _render_next_questions(questions) -> None:
    if not questions:
        return
    st.subheader("Para afinar la recomendación")
    for question in questions:
        st.markdown(f"- {question.question_text}")


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


# --------------------------------------------------------------------- #
# Render: sidebar + brand + chips + recent (Mejora 04 — onboarding).
# --------------------------------------------------------------------- #
def _render_brand_header() -> None:
    """Brand mark + serif logotype + uppercase tagline."""
    logo_svg = _load_logo_svg()
    st.sidebar.markdown(
        f'<div class="brand">'
        f'<div class="brand-mark">{logo_svg}</div>'
        f'<div class="brand-text">'
        f'<span class="brand-title">HaciendaGPT</span>'
        f'<span class="brand-subtitle">Asistente fiscal con citas</span>'
        f'</div></div>',
        unsafe_allow_html=True,
    )


def _render_sidebar_scope() -> None:
    """List of topics the assistant can answer about, rendered as chips
    so the user immediately understands the scope."""
    chips_html = "".join(
        f'<span class="chip">{html.escape(t)}</span>' for t in _TOPICS_SUPPORTED
    )
    st.sidebar.markdown(
        f'<div class="scope-label">Qué cubre hoy</div>'
        f'<div class="chip-row">{chips_html}</div>',
        unsafe_allow_html=True,
    )


def _render_sidebar_recents() -> None:
    """Show the last few user prompts as captions. Not interactive in
    this pass — re-submitting requires more session-state plumbing than
    fits this redesign."""
    user_prompts: list[str] = [
        m["content"] for m in st.session_state.get("messages", []) if m["role"] == "user"
    ]
    if not user_prompts:
        return
    st.sidebar.markdown(
        '<div class="scope-label">Recientes</div>', unsafe_allow_html=True
    )
    for q in reversed(user_prompts[-5:]):
        snippet = q if len(q) <= 90 else q[:87].rstrip() + "…"
        st.sidebar.caption(f"• {snippet}")


def _render_sidebar_disclaimer() -> None:
    st.sidebar.markdown(
        f'<div class="disclaimer">{_DISCLAIMER_FULL}</div>',
        unsafe_allow_html=True,
    )


def _render_sidebar_new_consultation() -> None:
    if st.sidebar.button("➕ Nueva consulta", use_container_width=True):
        # A fresh case_id + a wiped message log is enough — we deliberately
        # do not delete the persisted SQLite case (the audit trail stays
        # intact even when the user starts over).
        st.session_state["messages"] = [_initial_greeting()]
        st.session_state["case_id"] = f"case_{uuid4().hex}"
        st.session_state.pop("api_case_id", None)
        st.session_state.pop("pending_query", None)
        st.rerun()


# --------------------------------------------------------------------- #
# Render: suggested-question chips above the input (Mejora 04).
# --------------------------------------------------------------------- #
def _render_suggested_questions() -> str | None:
    """Render the 4 chips as native st.buttons inside a scoped div so the
    CSS in style.css can chip them. Returns the chosen question text,
    or None when nothing was clicked this rerun."""
    st.markdown('<div class="suggested-chips">', unsafe_allow_html=True)
    cols = st.columns(len(_SUGGESTED_QUESTIONS))
    chosen: str | None = None
    for col, q in zip(cols, _SUGGESTED_QUESTIONS, strict=False):
        with col:
            if st.button(q, key=f"chip-{hash(q)}", use_container_width=True):
                chosen = q
    st.markdown('</div>', unsafe_allow_html=True)
    return chosen


# --------------------------------------------------------------------- #
# Render: legal disclaimer below the chat input (Mejora 06).
# --------------------------------------------------------------------- #
def _render_input_disclaimer() -> None:
    st.markdown(
        f'<div class="disclaimer">{_DISCLAIMER_FULL}</div>',
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------- #
# Initial greeting kept in a helper so "Nueva consulta" can reuse it.
# --------------------------------------------------------------------- #
def _initial_greeting() -> dict[str, str]:
    return {
        "role": "assistant",
        "content": (
            "Hola, soy HaciendaGPT. Puedo ayudarte con IRPF, IVA, autónomos, "
            "Modelo 720 y otras consultas fiscales. Cuéntame tu caso y citaré "
            "la normativa aplicable."
        ),
    }


# --------------------------------------------------------------------- #
# Main orchestration.
# --------------------------------------------------------------------- #
def main():
    configure_logging()
    st.set_page_config(
        page_title="HaciendaGPT",
        page_icon="📑",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    _inject_theme()

    chain = load_chain()
    store = load_case_store()
    extractor = load_fact_extractor()
    case_id = _ensure_case_id()
    bot_avatar = _load_logo_data_uri()

    if "messages" not in st.session_state:
        st.session_state["messages"] = [_initial_greeting()]

    # ---- Sidebar ------------------------------------------------------
    _render_brand_header()
    _render_sidebar_new_consultation()
    _render_sidebar_scope()
    _render_sidebar_recents()
    _render_sidebar_disclaimer()

    # ---- Main column --------------------------------------------------
    # The trust-first redesign lives in the main column. We render the
    # chat log first, then the suggested-question chips just above the
    # input, then the disclaimer below.
    for message in st.session_state.messages:
        avatar = bot_avatar if message["role"] == "assistant" else None
        with st.chat_message(message["role"], avatar=avatar):
            st.markdown(message["content"])
            # Replay any persisted trust strip / sources for this turn.
            replay = message.get("__envelope")
            if replay is not None:
                _render_trust_strip(replay)
                _render_source_cards(replay)

    suggested = _render_suggested_questions()

    # The chat input or a suggested-question chip both produce a query;
    # the chip path pre-seeds it via session_state and triggers a rerun.
    typed_query = st.chat_input("Pregúntame lo que quieras")
    query: str | None = suggested or typed_query

    _render_input_disclaimer()

    if not query:
        return

    prior_history = [
        {"role": m["role"], "content": m["content"]} for m in st.session_state.messages
    ]
    st.session_state.messages.append({"role": "user", "content": query})
    with st.chat_message("user"):
        st.markdown(query)

    with st.chat_message("assistant", avatar=bot_avatar):
        history = _build_chat_history(st.session_state.messages[:-1])
        with st.spinner("Consultando la normativa…"):
            envelope = answer_with_grounding(chain, query=query, chat_history=history)
        response = envelope.answer
        st.markdown(response)
        _render_trust_strip(envelope)
        _render_source_cards(envelope)

    # Persist the envelope so the trust strip + source cards survive a
    # rerun (e.g. when the user clicks a suggested-question chip). Only
    # the envelope is stored; the message log keeps its plain-text role.
    st.session_state.messages.append(
        {"role": "assistant", "content": response, "__envelope": envelope}
    )

    if UI_USE_API:
        try:
            turn = _api_process_turn(query, chat_history=prior_history)
            st.caption(f"API case_id: {turn['case_id']}")
        except Exception as exc:
            st.error(f"Error llamando API backend: {exc}. Usando fallback local.")
            outcome = _persist_turn_local(
                store=store,
                case_id=case_id,
                user_input=query,
                assistant_output=response,
                extractor=extractor,
                chat_history=prior_history,
            )
            _render_debug(outcome.case_state)
            _render_obligation_cards(outcome.case_state)
            _render_next_questions(outcome.selected_questions)
    else:
        outcome = _persist_turn_local(
            store=store,
            case_id=case_id,
            user_input=query,
            assistant_output=response,
            extractor=extractor,
            chat_history=prior_history,
        )
        _render_debug(outcome.case_state)
        _render_obligation_cards(outcome.case_state)
        _render_next_questions(outcome.selected_questions)


if __name__ == "__main__":
    main()
