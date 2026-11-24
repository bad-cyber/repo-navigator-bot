import io
import logging
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlunparse

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".rs",
    ".java", ".kt", ".c", ".cpp", ".h", ".cs", ".php",
    ".rb", ".html", ".css", ".sql", ".sh", ".yml", ".yaml", ".md",
}

IGNORED_DIRS = {
    ".git", ".github", "node_modules", "vendor", "venv", ".venv",
    "__pycache__", "dist", "build", ".next", ".idea", ".vscode", "coverage",
}

MAX_SINGLE_FILE_BYTES = 150_000


@dataclass
class RepoFile:
    path: str
    language: str
    content: str


def _is_relevant_path(rel_path: str) -> bool:
    parts = Path(rel_path).parts
    if any(part in IGNORED_DIRS for part in parts):
        return False
    ext = Path(rel_path).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return False
    if rel_path.endswith(".min.js") or rel_path.endswith(".min.css"):
        return False
    return True


async def download_and_extract_repo(owner: str, repo: str) -> list[RepoFile]:
    """Скачивает zip-архив репозитория GitHub напрямую в память и извлекает файлы с кодом."""
    max_bytes = settings.max_repo_zip_mb * 1024 * 1024
    headers = {"User-Agent": "RepoNavigatorBot/0.1"}
    scheme = "http" + "s"

    resp = None
    async with httpx.AsyncClient(trust_env=False, follow_redirects=True, timeout=30.0) as client:
        for branch in ("main", "master"):
            zip_url = urlunparse(
                (scheme, "github.com", f"/{owner}/{repo}/archive/refs/heads/{branch}.zip", "", "", "")
            )
            logger.info("Запрос архива ветки '%s': %r", branch, zip_url)
            candidate = await client.get(zip_url, headers=headers)
            if candidate.status_code == 200:
                resp = candidate
                break
    if resp is None:
        raise ValueError("Репозиторий не найден, он приватный или ветка отличается от main/master.")
    if len(resp.content) > max_bytes:
        raise ValueError(f"Архив репозитория превышает лимит {settings.max_repo_zip_mb} МБ.")

    extracted_files: list[RepoFile] = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        for info in zf.infolist():
            if info.is_dir() or info.file_size == 0 or info.file_size > MAX_SINGLE_FILE_BYTES:
                continue

            raw_parts = Path(info.filename).parts
            if len(raw_parts) <= 1:
                continue
            rel_path = "/".join(raw_parts[1:])

            if not _is_relevant_path(rel_path):
                continue

            try:
                text = zf.read(info.filename).decode("utf-8")
            except UnicodeDecodeError:
                continue

            ext = Path(rel_path).suffix.lower().lstrip(".")
            extracted_files.append(RepoFile(path=rel_path, language=ext, content=text))

            if len(extracted_files) >= settings.max_files_per_repo:
                break

    logger.info("Извлечено релевантных файлов из %s/%s: %s", owner, repo, len(extracted_files))
    return extracted_files
