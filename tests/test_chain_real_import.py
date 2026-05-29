"""Real-package import smoke for `hacienda_gpt.llm.chain`.

The rest of the test suite uses `tests/conftest.py` to stub
`langchain.*`, `langchain_classic.*`, `langchain_community.*` etc. so that
imports succeed even when those heavy packages are not installed. That
stubbing is the right choice for CI-lite, but it hides regressions where
the production import path stops working against the *real* installed
versions — that is exactly how the
`from langchain.chains import create_history_aware_retriever` → 500-on-/qa
incident slipped through pre-existing tests (commit `19a1780`).

To close that gap we re-import the chain module **in a clean subprocess**
that never loads pytest's conftest.py, so the real packages from the
venv are exercised end-to-end at import time.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

_IMPORT_SCRIPT = """
import sys

import hacienda_gpt.llm.chain as chain

# Exercise the public symbols that api.py and ui/app.py rely on.
required = (
    "answer_with_grounding",
    "create_openai_chain",
    "default_grounding_gate",
    "FAISS_TRUSTED_INDEX",
    "FAISS_INDEX_PATH",
)

missing = [name for name in required if not hasattr(chain, name)]
if missing:
    print("MISSING_PUBLIC_SYMBOLS:" + ",".join(missing), file=sys.stderr)
    sys.exit(2)

print("ok")
"""


def _real_imports_available() -> bool:
    """The smoke is only meaningful when the real packages are installed."""
    try:
        import langchain_classic.chains  # noqa: F401
        import langchain_core.prompts  # noqa: F401
        import langchain_openai  # noqa: F401
    except Exception:
        return False
    return True


@pytest.mark.skipif(
    not _real_imports_available(),
    reason="real langchain_classic / langchain_core / langchain_openai not installed",
)
def test_chain_module_imports_in_clean_subprocess():
    # We run the import in a subprocess so that tests/conftest.py (which
    # injects stubs into sys.modules during pytest_sessionstart) does NOT
    # mask a broken import in production code.
    repo_root = Path(__file__).resolve().parent.parent
    env = {**os.environ, "PYTHONPATH": str(repo_root)}
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_SCRIPT],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=str(repo_root),
        env=env,
    )
    assert result.returncode == 0, (
        "hacienda_gpt.llm.chain failed to import with the real installed "
        "langchain packages — this is the contract that the /qa endpoint and "
        "the Streamlit UI depend on.\n"
        f"--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )
    assert result.stdout.strip().endswith("ok")
