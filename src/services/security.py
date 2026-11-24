import re
import time
from collections import defaultdict
from urllib.parse import urlparse

MAX_QUESTION_LENGTH = 400

# Строгий паттерн валидного публичного GitHub-репозитория (защита от SSRF)
GITHUB_REPO_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]{1,39})/([A-Za-z0-9_.-]{1,100})(?:\.git|/.*)?$"
)

INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|above|prior)\s+instructions",
    r"disregard\s+(all\s+)?(previous|above|prior)",
    r"system\s+prompt",
    r"<\|im_start\|>|<\|system\|>|\[INST\]",
    r"забудь\s+(все\s+)?(предыдущие\s+)?инструкции",
    r"игнорируй\s+(все\s+)?(предыдущие\s+)?(инструкции|правила)",
    r"выведи\s+(свой\s+)?системный\s+промпт",
    r"покажи\s+(свои\s+)?(инструкции|настройки|ключи|токен)",
]

_COMPILED_INJECTIONS = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]


def parse_github_url(raw_url: str) -> tuple[str, str] | None:
    """Извлекает (owner, repo) из URL GitHub или возвращает None, если ссылка невалидна."""
    cleaned = raw_url.strip().rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() not in ("github.com", "://github.com"):
        return None

    match = GITHUB_REPO_RE.match(cleaned)
    if not match:
        return None

    owner, repo = match.group(1), match.group(2)
    if repo.endswith(".git"):
        repo = repo[:-4]
    if owner in ("settings", "orgs", "topics", "explore", "marketplace"):
        return None
    return owner, repo


def validate_user_question(raw_text: str) -> tuple[bool, str]:
    """Очищает вопрос пользователя и проверяет на Prompt Injection."""
    cleaned = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", raw_text).strip()
    if len(cleaned) < 3:
        return False, "Напиши вопрос чуть подробнее (минимум 3 символа) 🙂"
    if len(cleaned) > MAX_QUESTION_LENGTH:
        return False, f"⚠️ Вопрос слишком длинный (максимум {MAX_QUESTION_LENGTH} символов)."

    for pattern in _COMPILED_INJECTIONS:
        if pattern.search(cleaned):
            return False, "🛡️ Запрос отклонён: обнаружена попытка инъекции инструкций."

    return True, cleaned


def sanitize_code_snippet(code: str) -> str:
    """Нейтрализует потенциальные Indirect Prompt Injections внутри чужого кода."""
    sanitized = code
    for pattern in _COMPILED_INJECTIONS:
        sanitized = pattern.sub("[REDACTED_INJECTION]", sanitized)
    return sanitized


class RateLimiter:
    """Раздельный антифлуд-лимитер для индексации репозиториев и вопросов."""

    def __init__(self) -> None:
        self._last_ask: dict[int, float] = defaultdict(float)
        self._last_index: dict[int, float] = defaultdict(float)

    def check_ask(self, user_id: int, interval: float = 3.0) -> tuple[bool, str]:
        now = time.monotonic()
        diff = now - self._last_ask[user_id]
        if diff < interval:
            return False, f"⏳ Подожди {int(interval - diff) + 1} сек. перед следующим вопросом."
        self._last_ask[user_id] = now
        return True, ""

    def check_index(self, user_id: int, interval: float = 15.0) -> tuple[bool, str]:
        now = time.monotonic()
        diff = now - self._last_index[user_id]
        if diff < interval:
            return False, f"⏳ Индексация доступна раз в {int(interval)} сек. Подожди ещё {int(interval - diff) + 1} сек."
        self._last_index[user_id] = now
        return True, ""


rate_limiter = RateLimiter()
