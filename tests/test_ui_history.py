from langchain_core.messages import AIMessage, HumanMessage

from hacienda_gpt.ui.app import _build_chat_history


def test_build_chat_history_maps_roles_correctly() -> None:
    messages = [
        {"role": "assistant", "content": "hola"},
        {"role": "user", "content": "qué tal"},
        {"role": "system", "content": "ignore me"},
    ]

    history = _build_chat_history(messages)

    assert history == [AIMessage(content="hola"), HumanMessage(content="qué tal")]
