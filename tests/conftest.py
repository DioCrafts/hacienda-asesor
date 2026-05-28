import sys
import types


def _ensure_module(name: str) -> types.ModuleType:
    mod = sys.modules.get(name)
    if mod is None:
        mod = types.ModuleType(name)
        sys.modules[name] = mod
    return mod


def pytest_sessionstart(session):
    # Minimal stubs for optional heavy dependencies so imports succeed in CI-lite envs.
    langchain = _ensure_module("langchain")
    langchain.chains = _ensure_module("langchain.chains")
    langchain.chains.create_history_aware_retriever = lambda *a, **k: object()
    langchain.chains.create_retrieval_chain = lambda *a, **k: object()

    combine_docs = _ensure_module("langchain.chains.combine_documents")
    combine_docs.create_stuff_documents_chain = lambda *a, **k: object()

    prompts = _ensure_module("langchain.prompts")

    class ChatPromptTemplate:
        @staticmethod
        def from_messages(messages):
            return messages

    class MessagesPlaceholder:
        def __init__(self, *_a, **_k):
            pass

    prompts.ChatPromptTemplate = ChatPromptTemplate
    prompts.MessagesPlaceholder = MessagesPlaceholder

    retrievers = _ensure_module("langchain.retrievers")
    retrievers.ContextualCompressionRetriever = object
    doc_compressors = _ensure_module("langchain.retrievers.document_compressors")
    doc_compressors.EmbeddingsFilter = object
    multiq = _ensure_module("langchain.retrievers.multi_query")

    class MultiQueryRetriever:
        @staticmethod
        def from_llm(*_a, **_k):
            return object()

    multiq.MultiQueryRetriever = MultiQueryRetriever

    # langchain 1.x moved the legacy chains to `langchain_classic`. The
    # production code now imports from there, so stub the mirror modules
    # to keep the CI-lite environment self-contained.
    lc_classic = _ensure_module("langchain_classic")
    lc_classic_chains = _ensure_module("langchain_classic.chains")
    lc_classic_chains.create_history_aware_retriever = lambda *a, **k: object()
    lc_classic_chains.create_retrieval_chain = lambda *a, **k: object()
    lc_classic_combine = _ensure_module("langchain_classic.chains.combine_documents")
    lc_classic_combine.create_stuff_documents_chain = lambda *a, **k: object()
    lc_classic_ret = _ensure_module("langchain_classic.retrievers")
    lc_classic_ret.ContextualCompressionRetriever = object
    lc_classic_doc_comp = _ensure_module(
        "langchain_classic.retrievers.document_compressors"
    )
    lc_classic_doc_comp.EmbeddingsFilter = object
    lc_classic_multiq = _ensure_module("langchain_classic.retrievers.multi_query")
    lc_classic_multiq.MultiQueryRetriever = MultiQueryRetriever

    # `chain.py` now also reads ChatPromptTemplate / MessagesPlaceholder
    # directly from langchain_core.prompts.
    lc_core_prompts = _ensure_module("langchain_core.prompts")
    lc_core_prompts.ChatPromptTemplate = ChatPromptTemplate
    lc_core_prompts.MessagesPlaceholder = MessagesPlaceholder

    lc_core_ret = _ensure_module("langchain_core.retrievers")
    lc_core_ret.BaseRetriever = object
    lc_core_run = _ensure_module("langchain_core.runnables")
    lc_core_run.Runnable = object

    lc_msgs = sys.modules.get("langchain_core.messages")
    if lc_msgs is None or not hasattr(lc_msgs, "BaseMessage"):
        lc_msgs = _ensure_module("langchain_core.messages")

        class _Msg:
            def __init__(self, content):
                self.content = content

            def __eq__(self, other):
                return self.__class__ is other.__class__ and self.content == other.content

        class BaseMessage(_Msg):
            pass

        class AIMessage(BaseMessage):
            pass

        class HumanMessage(BaseMessage):
            pass

        def _get_buffer_string(messages, human_prefix="Human", ai_prefix="AI"):
            return "\n".join(getattr(m, "content", str(m)) for m in messages)

        lc_msgs.BaseMessage = BaseMessage
        lc_msgs.AIMessage = AIMessage
        lc_msgs.HumanMessage = HumanMessage
        lc_msgs.get_buffer_string = _get_buffer_string

    vc = _ensure_module("langchain_community.vectorstores")

    class FAISS:
        @staticmethod
        def load_local(*_a, **_k):
            return object()

    vc.FAISS = FAISS

    lo = _ensure_module("langchain_openai")
    lo.ChatOpenAI = object
    lo.OpenAIEmbeddings = object

    try:
        import streamlit as st  # noqa: F401  # use real streamlit when installed (provides session_state, etc.)
    except ImportError:
        st = _ensure_module("streamlit")
    st.cache_resource = lambda fn: fn
    scrapy = _ensure_module("scrapy")

    class Spider:
        pass

    class Request:
        def __init__(self, *a, **k):
            self.args=a; self.kwargs=k

    scrapy.Spider = Spider
    scrapy.Request = Request

    http = _ensure_module("scrapy.http")
    http.Response = object
    le = _ensure_module("scrapy.linkextractors")

    class LinkExtractor:
        def __init__(self, *a, **k):
            pass

        def extract_links(self, response):
            return []

    le.LinkExtractor = LinkExtractor

    crawler = _ensure_module("scrapy.crawler")

    class CrawlerProcess:
        def __init__(self, settings=None):
            self.settings = settings

        def crawl(self, *a, **k):
            return None

        def start(self, install_signal_handlers=True):
            return None

    crawler.CrawlerProcess = CrawlerProcess
    spiders = _ensure_module("scrapy.spiders")
    spiders.Spider = Spider

    sp = _ensure_module("scrapy_playwright.page")
    sp.PageMethod = object
    pv = _ensure_module("pathvalidate")
    pv.sanitize_filepath = lambda value: value
    lc_docs = _ensure_module("langchain_core.documents")
    class Document:
        def __init__(self, page_content="", metadata=None):
            self.page_content = page_content
            self.metadata = metadata or {}
    lc_docs.Document = Document

    lc_emb = _ensure_module("langchain_core.embeddings")
    lc_emb.Embeddings = object

    loaders = _ensure_module("langchain_community.document_loaders")
    class DirectoryLoader:
        def __init__(self, *a, **k):
            pass
        def load(self):
            return []
    loaders.DirectoryLoader = DirectoryLoader

    emb = _ensure_module("langchain_community.embeddings")
    emb.GPT4AllEmbeddings = object

    ts = _ensure_module("langchain_text_splitters")
    ts.HTMLHeaderTextSplitter = lambda *a, **k: type("S", (), {"split_text": lambda self, t: []})()
    class RecursiveCharacterTextSplitter:
        def __init__(self, *a, **k):
            pass
        def split_documents(self, docs):
            return docs
    ts.RecursiveCharacterTextSplitter = RecursiveCharacterTextSplitter
