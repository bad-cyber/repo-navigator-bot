import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

# Подставляем тестовые переменные окружения до импорта config/rag_engine
os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("LLM_API_KEY", "test-openrouter-key")

from src.services.rag_engine import (  # noqa: E402
    _adjust_score_for_code,
    _cleanup_expired_repos,
    answer_code_question,
)


def test_adjust_score_penalizes_markdown_files() -> None:
    code_cand = {"file_path": "src/security.py"}
    md_cand = {"file_path": "README.md"}

    assert _adjust_score_for_code(code_cand, 0.80) == pytest.approx(0.80)
    assert _adjust_score_for_code(md_cand, 0.80) == pytest.approx(0.55)


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
        answer, chunks = await answer_code_question("bad-cyber", "demo", "Как работает авторизация?")

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
        answer, chunks = await answer_code_question("bad-cyber", "demo", "Где логин?")

    assert "src/auth.py:10-25" in answer
    assert len(chunks) == 1
    assert create_mock.await_count == 2


@pytest.mark.asyncio
async def test_answer_code_question_graceful_fallback_when_all_llms_fail() -> None:
    point = SimpleNamespace(
        payload={
            "file_path": "src/main.py",
            "start_line": 1,
            "end_line": 12,
            "content": "async def main(): pass",
        }
    )
    mock_qdrant = AsyncMock()
    mock_qdrant.query_points = AsyncMock(return_value=SimpleNamespace(points=[point]))
    create_mock = AsyncMock(side_effect=RuntimeError("Service Unavailable"))

    with (
        patch("src.services.rag_engine.models.encode_query", return_value=[0.1, 0.2]),
        patch("src.services.rag_engine.models.rerank", return_value=[0.9]),
        patch("src.services.rag_engine.AsyncQdrantClient", return_value=mock_qdrant),
        patch("src.services.rag_engine.llm_client.chat.completions.create", create_mock),
    ):
        answer, chunks = await answer_code_question("bad-cyber", "demo", "Где точка входа?")

    assert "временно перегружен" in answer
    assert len(chunks) == 1
