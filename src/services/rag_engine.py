import logging
import time
import uuid
from operator import itemgetter

from openai import AsyncOpenAI
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PointStruct,
    Range,
    VectorParams,
)
from sentence_transformers import CrossEncoder, SentenceTransformer

from src.config import settings
from src.services.chunker import CodeChunk, chunk_repo_file
from src.services.repo_loader import download_and_extract_repo
from src.services.security import sanitize_code_snippet

logger = logging.getLogger(__name__)

FALLBACK_FREE_MODELS = (
    "qwen/qwen-2.5-coder-32b-instruct:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-3-27b-it:free",
)

llm_client = AsyncOpenAI(
    api_key=settings.llm_api_key,
    base_url=settings.llm_base_url,
)


class CodeModelRegistry:
    """Синглтон для локальных моделей эмбеддингов и реранкера."""

    def __init__(self) -> None:
        self._embedder: SentenceTransformer | None = None
        self._reranker: CrossEncoder | None = None

    @property
    def embedder(self) -> SentenceTransformer:
        if self._embedder is None:
            self._embedder = SentenceTransformer(settings.embedding_model)
        return self._embedder

    @property
    def reranker(self) -> CrossEncoder:
        if self._reranker is None:
            self._reranker = CrossEncoder(settings.reranker_model)
        return self._reranker
    def encode_passages(self, texts: list[str]) -> list[list[float]]:
        prefixed = [f"passage: {t}" for t in texts]
        vectors = self.embedder.encode(prefixed, normalize_embeddings=True)
        return [v.tolist() for v in vectors]

    def encode_query(self, query: str) -> list[float]:
        vector = self.embedder.encode(f"query: {query}", normalize_embeddings=True)
        return vector.tolist()

    def rerank(self, query: str, passages: list[str]) -> list[float]:
        pairs = [[query, p] for p in passages]
        scores = self.reranker.predict(pairs)
        return [float(s) for s in scores]


models = CodeModelRegistry()


async def _ensure_collection(qdrant: AsyncQdrantClient) -> None:
    collections = await qdrant.get_collections()
    existing = [c.name for c in collections.collections]
    if settings.qdrant_collection not in existing:
        dim = models.embedder.get_embedding_dimension()
        await qdrant.create_collection(
            collection_name=settings.qdrant_collection,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )


async def _cleanup_expired_repos(qdrant: AsyncQdrantClient) -> None:
    """Удаляет из Qdrant чанки репозиториев, проиндексированных более index_ttl_seconds назад."""
    cutoff_ts = int(time.time()) - settings.index_ttl_seconds
    expired_filter = Filter(must=[FieldCondition(key="indexed_at", range=Range(lt=cutoff_ts))])
    await qdrant.delete(
        collection_name=settings.qdrant_collection,
        points_selector=FilterSelector(filter=expired_filter),
    )


async def index_github_repo(owner: str, repo: str) -> tuple[int, int]:
    """Скачивает репозиторий, разбивает на AST-чанки и индексирует в Qdrant."""
    repo_id = f"{owner.lower()}/{repo.lower()}"
    repo_files = await download_and_extract_repo(owner, repo)
    if not repo_files:
        raise ValueError("В репозитории не найдено подходящих файлов с исходным кодом.")

    all_chunks: list[CodeChunk] = []
    for rf in repo_files:
        all_chunks.extend(chunk_repo_file(rf))

    if not all_chunks:
        raise ValueError("Не удалось извлечь текстовые фрагменты кода из репозитория.")

    all_chunks = all_chunks[:400]
    indexed_at = int(time.time())

    passages = [
        f"Файл: {c.file_path} (строки {c.start_line}-{c.end_line})\n{c.content}"
        for c in all_chunks
    ]
    vectors = models.encode_passages(passages)
    points: list[PointStruct] = []
    for chunk, vec in zip(all_chunks, vectors):
        point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{repo_id}:{chunk.file_path}:{chunk.start_line}"))
        points.append(
            PointStruct(
                id=point_id,
                vector=vec,
                payload={
                    "repo_id": repo_id,
                    "file_path": chunk.file_path,
                    "language": chunk.language,
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "indexed_at": indexed_at,
                    "content": sanitize_code_snippet(chunk.content),
                },
            )
        )

    qdrant = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        await _ensure_collection(qdrant)
        await _cleanup_expired_repos(qdrant)
        repo_filter = Filter(must=[FieldCondition(key="repo_id", match=MatchValue(value=repo_id))])
        await qdrant.delete(
            collection_name=settings.qdrant_collection,
            points_selector=FilterSelector(filter=repo_filter),
        )
        await qdrant.upsert(collection_name=settings.qdrant_collection, points=points)
    finally:
        await qdrant.close()

    return len(repo_files), len(points)


def _adjust_score_for_code(candidate: dict, raw_score: float) -> float:
    """Слегка понижает вес Markdown-документации, чтобы в топ попадал исходный код."""
    file_path = str(candidate.get("file_path", "")).lower()
    if file_path.endswith(".md"):
        return raw_score - 0.25
    return raw_score


async def answer_code_question(owner: str, repo: str, question: str) -> tuple[str, list[dict]]:
    """Находит релевантные фрагменты кода и формирует ответ LLM со ссылками на строки."""
    repo_id = f"{owner.lower()}/{repo.lower()}"
    query_vec = models.encode_query(question)
    repo_filter = Filter(must=[FieldCondition(key="repo_id", match=MatchValue(value=repo_id))])

    qdrant = AsyncQdrantClient(url=settings.qdrant_url)
    try:
        await _cleanup_expired_repos(qdrant)
        response = await qdrant.query_points(
            collection_name=settings.qdrant_collection,
            query=query_vec,
            query_filter=repo_filter,
            limit=15,
            with_payload=True,
        )
    finally:
        await qdrant.close()

    candidates = [p.payload for p in response.points if p.payload]
    if not candidates:
        return (
            "⏳ Срок хранения индекса этого репозитория (2 часа) истёк или фрагменты не найдены. "
            "Отправь ссылку на репозиторий ещё раз для свежей индексации.",
            [],
        )

    passages = [f"{c['file_path']}:\n{c['content']}" for c in candidates]
    raw_scores = models.rerank(question, passages)
    adjusted = [_adjust_score_for_code(cand, sc) for cand, sc in zip(candidates, raw_scores)]
    ranked = sorted(zip(candidates, adjusted), key=itemgetter(1), reverse=True)
    top_chunks = [item for item, _ in ranked[:4]]

    context_blocks = [
        f"--- Файл: {c['file_path']} (строки {c['start_line']}-{c['end_line']}) ---\n{c['content']}"
        for c in top_chunks
    ]
    system_prompt = (
        "Ты Senior Software Engineer и AI-навигатор по кодовой базе. "
        "Отвечай на вопрос пользователя на русском языке, опираясь ТОЛЬКО на переданные фрагменты кода. "
        "Обязательно ссылайся на конкретные файлы и номера строк (например, `src/main.py:12-30`). "
        "Если в приведённом коде нет ответа, честно скажи об этом и не придумывай несуществующие функции."
    )
    user_prompt = f"Репозиторий: {owner}/{repo}\nВопрос: {question}\n\nФрагменты кода:\n" + "\n\n".join(context_blocks)

    candidate_models = list(dict.fromkeys([settings.llm_model, *FALLBACK_FREE_MODELS]))
    answer_text = ""

    for model_name in candidate_models:
        try:
            resp = await llm_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
                max_tokens=900,
                timeout=25.0,
            )
            choices = list(resp.choices or [])
            if choices:
                first_choice = choices.pop(0)
                text = (first_choice.message.content or "").strip()
                if text:
                    answer_text = text
                    break
        except Exception as exc:
            logger.warning("Модель %s недоступна (%s), пробую следующую...", model_name, exc)

    if not answer_text:
        answer_text = (
            "⚠️ Бесплатный API языковой модели временно перегружен, но я нашёл самые релевантные "
            "участки кода в репозитории — смотри ссылки на файлы и строки ниже."
        )

    return answer_text, top_chunks
