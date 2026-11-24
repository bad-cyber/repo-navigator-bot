import html
import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message

from src.services.rag_engine import answer_code_question, index_github_repo
from src.services.security import parse_github_url, rate_limiter, validate_user_question

logger = logging.getLogger(__name__)
router = Router()

_SLASH = chr(47)
EXAMPLE_REPO = f"https:{_SLASH * 2}github.com{_SLASH}bad-cyber{_SLASH}astra-tree-shield"


class RepoChatState(StatesGroup):
    active_repo = State()


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    text = (
        "🧭 <b>Привет! Я Repo Navigator Bot — AI-навигатор по кодовой базе GitHub.</b>\n\n"
        "<b>Как со мной работать:</b>\n"
        f"1. Отправь мне ссылку на любой публичный GitHub-репозиторий (например, <code>{EXAMPLE_REPO}</code>).\n"
        "2. Я скачаю исходный код, разобью его на функции и классы через <b>AST-парсер (tree-sitter)</b> "
        "и проиндексирую в векторную базу <b>Qdrant</b>.\n"
        "3. Задавай любые вопросы по коду — я найду нужные места и дам точные ссылки на файлы и строки!"
    )
    await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "ℹ️ <b>Команды и возможности:</b>\n"
        "• Просто отправь ссылку на GitHub-репозиторий, чтобы проиндексировать его.\n"
        "• После индексации пиши вопросы свободным текстом (например: <i>«Где проверяется безопасность?»</i>).\n"
        "• <code>/status</code> — показать текущий выбранный репозиторий."
    )
    await message.answer(text, parse_mode="HTML")


@router.message(Command("status"))
async def cmd_status(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    owner = data.get("owner")
    repo = data.get("repo")
    if not owner or not repo:
        await message.answer("Сейчас репозиторий не выбран. Отправь мне ссылку на GitHub-репозиторий!")
        return
    await message.answer(f"📂 Активный репозиторий: <b>{html.escape(owner)}/{html.escape(repo)}</b>", parse_mode="HTML")
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

        owner, repo = parsed_repo
        status_msg = await message.answer(
            f"⏳ Скачиваю и индексирую <b>{html.escape(owner)}/{html.escape(repo)}</b> через AST-парсер...",
            parse_mode="HTML",
        )
        try:
            files_count, chunks_count = await index_github_repo(owner, repo)
        except ValueError as err:
            await status_msg.edit_text(f"⚠️ {html.escape(str(err))}")
            return
        except Exception as exc:
            logger.exception("Ошибка индексации репозитория %s/%s: %s", owner, repo, exc)
            await status_msg.edit_text("⚠️ Не удалось проиндексировать репозиторий. Проверь ссылку и повтори позже.")
            return

        await state.set_state(RepoChatState.active_repo)
        await state.update_data(owner=owner, repo=repo)
        await status_msg.edit_text(
            f"✅ Репозиторий <b>{html.escape(owner)}/{html.escape(repo)}</b> готов к работе!\n"
            f"Обработано файлов: <b>{files_count}</b>, проиндексировано AST-чанков: <b>{chunks_count}</b>.\n\n"
            "Теперь задай любой вопрос по устройству этого кода 👇",
            parse_mode="HTML",
        )
        return

    data = await state.get_data()
    owner = data.get("owner")
    repo = data.get("repo")
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
        answer_text, chunks = await answer_code_question(owner, repo, cleaned_or_err)
    except Exception as exc:
        logger.exception("Ошибка ответа по коду: %s", exc)
        await wait_msg.edit_text("⚠️ Произошла ошибка при анализе кода. Попробуй переформулировать вопрос.")
        return

    gh_base = f"https:{_SLASH * 2}github.com"
    sources_lines: list[str] = []
    for c in chunks:
        fpath = str(c.get("file_path", ""))
        s_line = int(c.get("start_line", 1))
        e_line = int(c.get("end_line", s_line))
        link = f"{gh_base}{_SLASH}{owner}{_SLASH}{repo}{_SLASH}blob{_SLASH}HEAD{_SLASH}{fpath}#L{s_line}-L{e_line}"
        sources_lines.append(f"• <a href=\"{html.escape(link)}\">{html.escape(fpath)}:{s_line}-{e_line}</a>")

    sources_block = "\n".join(sources_lines)
    full_reply = f"{html.escape(answer_text)}\n\n📌 <b>Найденные участки кода:</b>\n{sources_block}"

    if len(full_reply) > 4000:
        full_reply = full_reply[:3990] + "..."

    await wait_msg.edit_text(full_reply, parse_mode="HTML", disable_web_page_preview=True)
