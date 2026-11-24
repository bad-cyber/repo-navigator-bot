import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("LLM_API_KEY", "test-openrouter-key")

from src.bot.handlers import _format_telegram_html  # noqa: E402
from src.services.rag_engine import (  # noqa: E402
    _REPO_METADATA_CACHE,
    RepoCacheEntry,
    _adjust_score_for_code,
    _cleanup_expired_repos,
    answer_code_question,
    audit_repo_security,
    get_cached_repo_metadata,
)
from src.services.security import SecurityFinding  # noqa: E402


def test_adjust_score_penalizes_markdown_files() -> None:
    code_cand = {"file_path": "src/security.py"}
    md_cand = {"file_path": "README.md"}

    assert _adjust_score_for_code(code_cand, 0.80) == pytest.approx(0.80)
    assert _adjust_score_for_code(md_cand, 0.80) == pytest.approx(0.55)


def test_metadata_cache_prunes_expired_entries() -> None:
    _REPO_METADATA_CACHE.clear()
    _REPO_METADATA_CACHE["bad-cyber/old@main"] = RepoCacheEntry(
        branch="main",
        file_paths=["src/app.py"],
        findings=[],
        indexed_at=int(time.time()) - 10_000,
    )
    assert get_cached_repo_metadata("bad-cyber", "old", "main") is None


def test_format_telegram_html_creates_clickable_github_links() -> None:
    raw = "Проверь функцию в `src/main.py:10-25` и тег <script>."
    rendered = _format_telegram_html(raw, "bad-cyber", "demo", "dev")
    assert "blob/dev/src/main.py#L10-L25" in rendered
    assert "&lt;script&gt;" in rendered


@pytest.mark.asyncio
async def test_cleanup_expired_repos_calls_qdrant_delete() -> None:
    mock_qdrant = AsyncMock()
    await _cleanup_expired_repos(mock_qdrant)
    mock_qdrant.delete.assert_awaited_once()
@pytest.mark.asyncio
async def test_answer_code_question_returns_expired_message_when_empty() -> None:
    mock_qdrant = AsyncMock()
    mock_qdrant.query_points = AsyncMock(return_value=SimpleNamespace(points=[]))

    with (
        patch("src.services.rag_engine.models.encode_query", return_value=[0.1, 0.2]),
        patch("src.services.rag_engine.AsyncQdrantClient", return_value=mock_qdrant),
    ):
        answer, chunks = await answer_code_question("bad-cyber", "demo", "Как работает авторизация?", branch="main")

    assert "Срок хранения индекса" in answer
    assert chunks == []
    mock_qdrant.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_answer_code_question_uses_fallback_llm_on_primary_error() -> None:
    point = SimpleNamespace(
        payload={
            "file_path": "src/auth.py",
            "start_line": 10,
            "end_line": 25,
            "content": "def login():\n    return True",
        }
    )
    mock_qdrant = AsyncMock()
    mock_qdrant.query_points = AsyncMock(return_value=SimpleNamespace(points=[point]))

    llm_ok_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Авторизация в `src/auth.py:10-25`."))]
    )
    create_mock = AsyncMock(side_effect=[RuntimeError("429 Rate Limit"), llm_ok_response])

    with (
        patch("src.services.rag_engine.models.encode_query", return_value=[0.1, 0.2]),
        patch("src.services.rag_engine.models.rerank", return_value=[0.95]),
        patch("src.services.rag_engine.AsyncQdrantClient", return_value=mock_qdrant),
        patch("src.services.rag_engine.llm_client.chat.completions.create", create_mock),
    ):
        answer, chunks = await answer_code_question("bad-cyber", "demo", "Где логин?", branch="main")

    assert "src/auth.py:10-25" in answer
    assert len(chunks) == 1
    assert create_mock.await_count == 2


@pytest.mark.asyncio
async def test_audit_repo_security_combines_static_findings_and_llm() -> None:
    _REPO_METADATA_CACHE["bad-cyber/demo@main"] = RepoCacheEntry(
        branch="main",
        file_paths=["src/runner.py"],
        findings=[
            SecurityFinding(
                severity="HIGH",
                category="Динамическое выполнение кода (eval/exec)",
                file_path="src/runner.py",
                line_num=14,
                snippet="eval(payload)",
            )
        ],
        indexed_at=int(time.time()),
    )
    point = SimpleNamespace(
        payload={
            "file_path": "src/runner.py",
            "start_line": 10,
            "end_line": 18,
            "content": "def run(payload):\n    return eval(payload)",
        }
    )
    mock_qdrant = AsyncMock()
    mock_qdrant.query_points = AsyncMock(return_value=SimpleNamespace(points=[point]))
    llm_resp = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Обнаружен вызов `src/runner.py:14`."))]
    )

    with (
        patch("src.services.rag_engine.models.encode_query", return_value=[0.1, 0.2]),
        patch("src.services.rag_engine.models.rerank", return_value=[0.92]),
        patch("src.services.rag_engine.AsyncQdrantClient", return_value=mock_qdrant),
        patch("src.services.rag_engine.llm_client.chat.completions.create", AsyncMock(return_value=llm_resp)),
    ):
        report, findings, chunks = await audit_repo_security("bad-cyber", "demo", branch="main")

    assert "src/runner.py:14" in report
    assert len(findings) == 1
    assert len(chunks) == 1
