import logging
import time
import uuid
from dataclasses import dataclass
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
from src.services.security import (
    SecurityFinding,
    redact_secrets,
    sanitize_code_snippet,
    scan_file_for_risks,
)

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


@dataclass
class RepoCacheEntry:
    branch: str
    file_paths: list[str]
    findings: list[SecurityFinding]
    indexed_at: int


_REPO_METADATA_CACHE: dict[str, RepoCacheEntry] = {}


def _make_repo_id(owner: str, repo: str, branch: str | None = None) -> str:
    branch_part = (branch or "default").lower()
    return f"{owner.lower()}/{repo.lower()}@{branch_part}"
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


def _prune_metadata_cache() -> None:
    cutoff = int(time.time()) - settings.index_ttl_seconds
    expired_keys = [k for k, v in _REPO_METADATA_CACHE.items() if v.indexed_at < cutoff]
    for key in expired_keys:
        _REPO_METADATA_CACHE.pop(key, None)


def get_cached_repo_metadata(owner: str, repo: str, branch: str | None = None) -> RepoCacheEntry | None:
    _prune_metadata_cache()
    return _REPO_METADATA_CACHE.get(_make_repo_id(owner, repo, branch))


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
async def _generate_with_fallback(system_prompt: str, user_prompt: str, fallback_msg: str) -> str:
    candidate_models = list(dict.fromkeys([settings.llm_model, *FALLBACK_FREE_MODELS]))
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
                    return redact_secrets(text)
        except Exception as exc:
            logger.warning("Модель %s недоступна (%s), пробую следующую...", model_name, exc)
    return redact_secrets(fallback_msg)


async def index_github_repo(
    owner: str,
    repo: str,
    branch: str | None = None,
) -> tuple[int, int, str]:
    """Скачивает репозиторий, сканирует риски, разбивает на AST-чанки и индексирует в Qdrant."""
    repo_files, resolved_branch = await download_and_extract_repo(owner, repo, branch=branch)
    if not repo_files:
        raise ValueError("В репозитории не найдено подходящих файлов с исходным кодом.")

    repo_id = _make_repo_id(owner, repo, resolved_branch)
    all_chunks: list[CodeChunk] = []
    all_findings: list[SecurityFinding] = []
    file_paths: list[str] = []

    for rf in repo_files:
        file_paths.append(rf.path)
        all_findings.extend(scan_file_for_risks(rf.path, rf.content))
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
        clean_content = redact_secrets(sanitize_code_snippet(chunk.content))
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
                    "content": clean_content,
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

    _prune_metadata_cache()
    _REPO_METADATA_CACHE[repo_id] = RepoCacheEntry(
        branch=resolved_branch,
        file_paths=sorted(file_paths),
        findings=all_findings,
        indexed_at=indexed_at,
    )
    return len(repo_files), len(points), resolved_branch


def _adjust_score_for_code(candidate: dict, raw_score: float) -> float:
    """Слегка понижает вес Markdown-документации, чтобы в топ попадал исходный код."""
    file_path = str(candidate.get("file_path", "")).lower()
    if file_path.endswith(".md"):
        return raw_score - 0.25
    return raw_score


async def _retrieve_top_chunks(repo_id: str, query: str, limit: int = 4) -> list[dict]:
    query_vec = models.encode_query(query)
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
        return []

    passages = [f"{c['file_path']}:\n{c['content']}" for c in candidates]
    raw_scores = models.rerank(query, passages)
    adjusted = [_adjust_score_for_code(cand, sc) for cand, sc in zip(candidates, raw_scores)]
    ranked = sorted(zip(candidates, adjusted), key=itemgetter(1), reverse=True)
    return [item for item, _ in ranked[:limit]]


async def answer_code_question(
    owner: str,
    repo: str,
    question: str,
    branch: str | None = None,
) -> tuple[str, list[dict]]:
    """Находит релевантные фрагменты кода и формирует ответ LLM со ссылками на строки."""
    repo_id = _make_repo_id(owner, repo, branch)
    top_chunks = await _retrieve_top_chunks(repo_id, question, limit=4)
    if not top_chunks:
        return (
            "⏳ Срок хранения индекса этого репозитория (2 часа) истёк или фрагменты не найдены. "
            "Отправь ссылку на репозиторий ещё раз для свежей индексации.",
            [],
        )

    context_blocks = [
        f"--- Файл: {c['file_path']} (строки {c['start_line']}-{c['end_line']}) ---\n{c['content']}"
        for c in top_chunks
    ]
    system_prompt = (
        "Ты Senior Software Engineer и AI-навигатор по кодовой базе. "
        "Отвечай на вопрос пользователя на русском языке, опираясь ТОЛЬКО на переданные фрагменты кода. "
        "Обязательно ссылайся на конкретные файлы и номера строк (например, `src/main.py:12-30`). "
        "Никогда не выводи секретные токены или пароли в открытом виде. "
        "Если в приведённом коде нет ответа, честно скажи об этом и не придумывай несуществующие функции."
    )
    user_prompt = (
        f"Репозиторий: {owner}/{repo} (ветка: {branch or 'default'})\n"
        f"Вопрос: {question}\n\nФрагменты кода:\n" + "\n\n".join(context_blocks)
    )
    fallback_msg = (
        "⚠️ Бесплатный API языковой модели временно перегружен, но я нашёл самые релевантные "
        "участки кода в репозитории — смотри ссылки на файлы и строки ниже."
    )
    answer_text = await _generate_with_fallback(system_prompt, user_prompt, fallback_msg)
    return answer_text, top_chunks


async def audit_repo_security(
    owner: str,
    repo: str,
    branch: str | None = None,
) -> tuple[str, list[SecurityFinding], list[dict]]:
    """Проводит статический и RAG-анализ безопасности репозитория и формирует LLM-отчёт."""
    repo_id = _make_repo_id(owner, repo, branch)
    cached = get_cached_repo_metadata(owner, repo, branch)
    findings = cached.findings if cached else []

    security_query = (
        "authentication authorization secret api_key password token sql eval exec subprocess shell"
    )
    top_chunks = await _retrieve_top_chunks(repo_id, security_query, limit=4)
    if not top_chunks and not findings:
        return (
            "⏳ Индекс репозитория не найден или истёк (2 часа). "
            "Отправь ссылку на репозиторий ещё раз перед запуском /audit.",
            [],
            [],
        )

    findings_summary = (
        "\n".join(
            f"- [{f.severity}] {f.category} в {f.file_path}:{f.line_num} -> `{f.snippet}`"
            for f in findings[:15]
        )
        if findings
        else "Статический сканер не обнаружил явных паттернов уязвимостей или утечек ключей."
    )
    context_blocks = [
        f"--- Файл: {c['file_path']} (строки {c['start_line']}-{c['end_line']}) ---\n{c['content']}"
        for c in top_chunks
    ]
    system_prompt = (
        "Ты AppSec-инженер и аудитор безопасности кода. "
        "Проанализируй результаты статического сканирования и фрагменты кода репозитория. "
        "Дай краткий, структурированный отчёт на русском языке: 1) критичные и средние риски со ссылками на файлы и строки, "
        "2) оценку работы с секретами/внешними данными, 3) 2-3 практические рекомендации. "
        "Не выводи открытые ключи и не выдумывай уязвимости, которых нет в контексте."
    )
    user_prompt = (
        f"Репозиторий: {owner}/{repo} (ветка: {branch or 'default'})\n\n"
        f"Находки статического сканера:\n{findings_summary}\n\n"
        f"Релевантные участки кода:\n" + "\n\n".join(context_blocks)
    )
    fallback_msg = (
        "📋 Автоматический LLM-анализ временно недоступен, ниже приведены результаты "
        "статического сканера безопасности и ключевые участки кода."
    )
    report_text = await _generate_with_fallback(system_prompt, user_prompt, fallback_msg)
    return report_text, findings, top_chunks
