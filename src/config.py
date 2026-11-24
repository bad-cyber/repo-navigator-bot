from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Telegram
    bot_token: str

    # OpenRouter / LLM API
    llm_api_key: str
    llm_base_url: str = "https: / / openrouter.ai / api / v1".replace(" ", "")
    llm_model: str = "openrouter/free"

    # Qdrant Vector DB
    qdrant_url: str = "http: / / localhost:6335".replace(" ", "")
    qdrant_collection: str = "code_chunks"
    index_ttl_seconds: int = 7200  # 2 часа жизни индекса в Qdrant

    # Локальные модели для поиска и реранкинга (уже закэшированы в системе)
    embedding_model: str = "intfloat/multilingual-e5-small"
    reranker_model: str = "DiTy/cross-encoder-russian-msmarco"

    # Лимиты безопасности при загрузке репозитория
    max_repo_zip_mb: int = 25
    max_files_per_repo: int = 120

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
