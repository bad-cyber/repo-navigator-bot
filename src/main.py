import asyncio
import logging
import socket

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage

from src.bot.handlers import router
from src.config import settings
from src.services.rag_engine import models

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    logger.info("Загрузка локальных ML-моделей (эмбеддинги и реранкер)...")
    _ = models.embedder
    _ = models.reranker
    logger.info("Модели готовы к работе.")

    session = AiohttpSession()
    session._connector_init["family"] = socket.AF_INET

    bot = Bot(token=settings.bot_token, session=session)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    logger.info("Запуск Repo Navigator Bot...")
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
