import html
import logging
import re

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from src.services.rag_engine import (
    answer_code_question,
    audit_repo_security,
    get_cached_repo_metadata,
    index_github_repo,
)
from src.services.security import parse_github_url, rate_limiter, redact_secrets, validate_user_question

logger = logging.getLogger(__name__)
router = Router()

_SLASH = chr(47)
_GH_BASE = f"https:{_SLASH * 2}github.com"
EXAMPLE_REPO = f"{_GH_BASE}{_SLASH}bad-cyber{_SLASH}astra-tree-shield"

_INLINE_CODE_REF_RE = re.compile(
    r"`([A-Za-z0-9_./-]+\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?`"
)
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")


class RepoChatState(StatesGroup):
    active_repo = State()


def _build_github_blob_url(owner: str, repo: str, branch: str, file_path: str, start_line: int, end_line: int | None = None) -> str:
    anchor = f"#L{start_line}-L{end_line}" if end_line and end_line != start_line else f"#L{start_line}"
    return f"{_GH_BASE}{_SLASH}{owner}{_SLASH}{repo}{_SLASH}blob{_SLASH}{branch}{_SLASH}{file_path}{anchor}"


def _format_telegram_html(raw_text: str, owner: str, repo: str, branch: str) -> str:
    """Безопасно экранирует текст и превращает ссылки на файлы/строки в кликабельные ссылки GitHub."""
    safe_text = redact_secrets(raw_text)
    placeholders: list[str] = []

    def _replace_ref(match: re.Match[str]) -> str:
        fpath, s_str, e_str = match.group(1), match.group(2), match.group(3)
        s_line = int(s_str)
        e_line = int(e_str) if e_str else s_line
        url = _build_github_blob_url(owner, repo, branch, fpath, s_line, e_line)
        label = f"{fpath}:{s_line}-{e_line}" if e_str else f"{fpath}:{s_line}"
        token = f"@@PH_{len(placeholders)}@@"
        placeholders.append(f"<a href=\"{html.escape(url)}\">{html.escape(label)}</a>")
        return token

    def _replace_code(match: re.Match[str]) -> str:
        token = f"@@PH_{len(placeholders)}@@"
        placeholders.append(f"<code>{html.escape(match.group(1))}</code>")
        return token

    temp = _INLINE_CODE_REF_RE.sub(_replace_ref, safe_text)
    temp = _INLINE_CODE_RE.sub(_replace_code, temp)
    escaped = html.escape(temp)

    for idx, replacement in enumerate(placeholders):
        escaped = escaped.replace(f"@@PH_{idx}@@", replacement)
    return escaped
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    text = (
        "🧭 <b>Привет! Я Repo Navigator Bot — AI-навигатор по кодовой базе GitHub.</b>\n\n"
        "<b>Как со мной работать:</b>\n"
        f"1. Отправь мне ссылку на публичный GitHub-репозиторий или ветку (например, <code>{EXAMPLE_REPO}</code>).\n"
        "2. Я скачаю исходный код, разобью его через <b>AST-парсер (tree-sitter)</b> "
        "и проиндексирую в векторную базу <b>Qdrant</b>.\n"
        "3. Задавай вопросы по коду или используй команды <code>/tree</code> и <code>/audit</code>!"
    )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "ℹ️ <b>Команды и возможности:</b>\n"
        "• Отправь ссылку на GitHub-репозиторий (включая <code>/tree/&lt;branch&gt;</code>), чтобы проиндексировать его.\n"
        "• Пиши вопросы по коду свободным текстом — я отвечу со ссылками на строки GitHub.\n"
        "• <code>/tree</code> — показать структуру проиндексированных файлов.\n"
        "• <code>/audit</code> — запустить базовый аудит безопасности и поиск утечек ключей.\n"
        "• <code>/status</code> — показать текущий репозиторий и активную ветку."
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("status"))
async def cmd_status(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    owner, repo, branch = data.get("owner"), data.get("repo"), data.get("branch", "main")
    if not owner or not repo:
        await message.answer("Сейчас репозиторий не выбран. Отправь мне ссылку на GitHub-репозиторий!")
        return
    await message.answer(
        f"📂 Активный репозиторий: <b>{html.escape(owner)}/{html.escape(repo)}</b> "
        f"(ветка: <code>{html.escape(branch)}</code>)",
        parse_mode="HTML",
    )


@router.message(Command("tree"))
async def cmd_tree(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    owner, repo, branch = data.get("owner"), data.get("repo"), data.get("branch")
    if not owner or not repo:
        await message.answer("Сначала отправь ссылку на публичный GitHub-репозиторий 🔗")
        return

    cached = get_cached_repo_metadata(owner, repo, branch)
    if not cached or not cached.file_paths:
        await message.answer("⏳ Кэш структуры файлов истёк (2 часа). Отправь ссылку на репозиторий ещё раз.")
        return

    lines = [f"• <code>{html.escape(p)}</code>" for p in cached.file_paths[:80]]
    extra = f"\n<i>...и ещё {len(cached.file_paths) - 80} файлов</i>" if len(cached.file_paths) > 80 else ""
    reply = (
        f"🌳 <b>Файлы репозитория {html.escape(owner)}/{html.escape(repo)} ({html.escape(cached.branch)}):</b>\n\n"
        + "\n".join(lines)
        + extra
    )
    await message.answer(reply[:4000], parse_mode="HTML")
@router.message(Command("audit"))
async def cmd_audit(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    owner, repo, branch = data.get("owner"), data.get("repo"), data.get("branch")
    if not owner or not repo:
        await message.answer("Сначала отправь ссылку на публичный GitHub-репозиторий 🔗")
        return

    user_id = message.from_user.id if message.from_user else 0
    allowed, limit_msg = rate_limiter.check_ask(user_id, interval=5.0)
    if not allowed:
        await message.answer(limit_msg)
        return

    wait_msg = await message.answer("🛡️ Провожу статический и RAG-аудит безопасности репозитория...")
    try:
        report_text, findings, _ = await audit_repo_security(owner, repo, branch=branch)
    except Exception as exc:
        logger.exception("Ошибка аудита безопасности: %s", exc)
        await wait_msg.edit_text("⚠️ Не удалось выполнить аудит безопасности. Попробуй позже.")
        return

    resolved_branch = branch or "main"
    formatted_report = _format_telegram_html(report_text, owner, repo, resolved_branch)

    findings_lines: list[str] = []
    for f in findings[:8]:
        url = _build_github_blob_url(owner, repo, resolved_branch, f.file_path, f.line_num)
        findings_lines.append(
            f"• <b>[{html.escape(f.severity)}]</b> {html.escape(f.category)} — "
            f"<a href=\"{html.escape(url)}\">{html.escape(f.file_path)}:{f.line_num}</a>"
        )

    findings_block = (
        "\n\n🔎 <b>Находки статического сканера:</b>\n" + "\n".join(findings_lines)
        if findings_lines
        else "\n\n✅ Статический сканер не выявил типовых паттернов уязвимостей."
    )
    full_reply = f"🛡️ <b>Аудит безопасности {html.escape(owner)}/{html.escape(repo)}:</b>\n\n{formatted_report}{findings_block}"
    if len(full_reply) > 4000:
        full_reply = full_reply[:3990] + "..."

    await wait_msg.edit_text(full_reply, parse_mode="HTML", disable_web_page_preview=True)


@router.message(F.text)
async def handle_text_message(message: Message, state: FSMContext) -> None:
    raw_text = (message.text or "").strip()
    user_id = message.from_user.id if message.from_user else 0

    parsed_repo = parse_github_url(raw_text)
    if parsed_repo is not None:
        allowed, limit_msg = rate_limiter.check_index(user_id)
        if not allowed:
            await message.answer(limit_msg)
            return

        owner, repo, branch = parsed_repo
        branch_label = f" (ветка {branch})" if branch else ""
        status_msg = await message.answer(
            f"⏳ Скачиваю и индексирую <b>{html.escape(owner)}/{html.escape(repo)}{html.escape(branch_label)}</b>...",
            parse_mode="HTML",
        )
        try:
            files_count, chunks_count, resolved_branch = await index_github_repo(owner, repo, branch=branch)
        except ValueError as err:
            await status_msg.edit_text(f"⚠️ {html.escape(str(err))}")
            return
        except Exception as exc:
            logger.exception("Ошибка индексации репозитория %s/%s: %s", owner, repo, exc)
            await status_msg.edit_text("⚠️ Не удалось проиндексировать репозиторий. Проверь ссылку и повтори позже.")
            return

        await state.set_state(RepoChatState.active_repo)
        await state.update_data(owner=owner, repo=repo, branch=resolved_branch)
        await status_msg.edit_text(
            f"✅ Репозиторий <b>{html.escape(owner)}/{html.escape(repo)}</b> "
            f"(ветка <code>{html.escape(resolved_branch)}</code>) готов к работе!\n"
            f"Обработано файлов: <b>{files_count}</b>, проиндексировано AST-чанков: <b>{chunks_count}</b>.\n\n"
            "Задай вопрос по коду или вызови <code>/tree</code> / <code>/audit</code> 👇",
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    owner, repo, branch = data.get("owner"), data.get("repo"), data.get("branch")
    if not owner or not repo:
        await message.answer(
            f"Сначала отправь ссылку на публичный GitHub-репозиторий (например, <code>{EXAMPLE_REPO}</code>) 🔗",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        return

    allowed, limit_msg = rate_limiter.check_ask(user_id)
    if not allowed:
        await message.answer(limit_msg)
        return

    is_valid, cleaned_or_err = validate_user_question(raw_text)
    if not is_valid:
        await message.answer(cleaned_or_err)
        return

    wait_msg = await message.answer("🔍 Ищу релевантные функции и модули в кодовой базе...")
    try:
        answer_text, chunks = await answer_code_question(owner, repo, cleaned_or_err, branch=branch)
    except Exception as exc:
        logger.exception("Ошибка ответа по коду: %s", exc)
        await wait_msg.edit_text("⚠️ Произошла ошибка при анализе кода. Попробуй переформулировать вопрос.")
        return

    resolved_branch = branch or "main"
    formatted_answer = _format_telegram_html(answer_text, owner, repo, resolved_branch)

    sources_lines: list[str] = []
    for c in chunks:
        fpath = str(c.get("file_path", ""))
        s_line = int(c.get("start_line", 1))
        e_line = int(c.get("end_line", s_line))
        link = _build_github_blob_url(owner, repo, resolved_branch, fpath, s_line, e_line)
        sources_lines.append(f"• <a href=\"{html.escape(link)}\">{html.escape(fpath)}:{s_line}-{e_line}</a>")

    sources_block = "\n".join(sources_lines)
    full_reply = f"{formatted_answer}\n\n📌 <b>Найденные участки кода:</b>\n{sources_block}"

    if len(full_reply) > 4000:
        full_reply = full_reply[:3990] + "..."

    await wait_msg.edit_text(full_reply, parse_mode="HTML", disable_web_page_preview=True)
