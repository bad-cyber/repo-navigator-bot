import re
import time
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import urlparse

MAX_QUESTION_LENGTH = 400

GITHUB_REPO_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]{1,39})/([A-Za-z0-9_.-]{1,100})(?:\.git|/tree/([A-Za-z0-9_./-]+)|/.*)?$"
)

INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|above|prior)\s+instructions",
    r"disregard\s+(all\s+)?(previous|above|prior)",
    r"system\s+prompt",
    r"<\|im_start\|>|<\|system\|>|\[INST\]",
    r"забудь\s+(все\s+)?(предыдущие\s+)?инструкции",
    r"игнорируй\s+(все\s+)?(предыдущие\s+)?(инструкции|правила)",
    r"выведи\s+(свой\s+)?системный\s+промпт",
]

_COMPILED_INJECTIONS = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]


def parse_github_url(raw_url: str) -> tuple[str, str, str | None] | None:
    """Извлекает (owner, repo, branch) из URL GitHub или возвращает None."""
    cleaned = raw_url.strip().rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme not in ("http", "https") or parsed.netloc.lower() not in ("github.com", "://github.com"):
        return None

    match = GITHUB_REPO_RE.match(cleaned)
    if not match:
        return None

    owner, repo, branch = match.group(1), match.group(2), match.group(3)
    if repo.endswith(".git"):
        repo = repo[:-4]
    if owner in ("settings", "orgs", "topics", "explore", "marketplace"):
        return None
    return owner, repo, branch

@dataclass
class SecurityFinding:
    severity: str
    category: str
    file_path: str
    line_num: int
    snippet: str


VULNERABILITY_RULES: tuple[tuple[str, str, str], ...] = (
    ("HIGH", "Динамическое выполнение кода (eval/exec)", r"\b(eval|exec)\s*\("),
    ("HIGH", "Небезопасная десериализация (pickle/yaml.load)", r"\b(pickle\.loads|yaml\.load)\s*\("),
    ("HIGH", "Выполнение команд ОС (os.system / shell=True)", r"\b(os\.system\s*\(|shell\s*=\s*True)"),
    ("MEDIUM", "Потенциальный XSS (innerHTML / dangerouslySetInnerHTML)", r"\b(innerHTML|dangerouslySetInnerHTML)\b"),
    ("MEDIUM", "Возможная SQL-инъекция (f-строка в SQL-запросе)", r"(SELECT|INSERT|UPDATE|DELETE)\s+.+\{.+\}"),
    ("LOW", "Отключена проверка TLS-сертификата (verify=False)", r"\bverify\s*=\s*False\b"),
)

_COMPILED_VULNS = tuple(
    (sev, cat, re.compile(pat, re.IGNORECASE)) for sev, cat, pat in VULNERABILITY_RULES
)


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

_SENSITIVE_PREFIXES = (
    "sk-" + "or-v1-",
    "sk-" + "proj-",
    "gh" + "p_",
    "gh" + "o_",
    "AK" + "IA",
)


def redact_secrets(text: str) -> str:
    """Маскирует случайно попавшие в текст API-ключи и префиксы."""
    result = text
    for prefix in _SENSITIVE_PREFIXES:
        pattern = re.compile(re.escape(prefix) + r"[A-Za-z0-9_\-]{8,}")
        result = pattern.sub(prefix + "***REDACTED***", result)
    return result


def scan_file_for_risks(file_path: str, content: str) -> list[SecurityFinding]:
    """Проверяет строки исходного файла на типовые риски безопасности и утечки."""
    findings: list[SecurityFinding] = []
    for line_idx, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "/*")):
            continue

        for prefix in _SENSITIVE_PREFIXES:
            if prefix in stripped:
                findings.append(
                    SecurityFinding(
                        severity="CRITICAL",
                        category="Возможная утечка статического ключа в коде",
                        file_path=file_path,
                        line_num=line_idx,
                        snippet=redact_secrets(stripped[:120]),
                    )
                )

        for sev, category, regex in _COMPILED_VULNS:
            if regex.search(stripped):
                findings.append(
                    SecurityFinding(
                        severity=sev,
                        category=category,
                        file_path=file_path,
                        line_num=line_idx,
                        snippet=stripped[:120],
                    )
                )
    return findings


class RateLimiter:
    """Раздельный антифлуд-лимитер для индексации, вопросов и аудита."""

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
