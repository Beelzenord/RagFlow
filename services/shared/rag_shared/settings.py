from functools import lru_cache
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # Postgres
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "postgres"
    postgres_user: str = "postgres"
    postgres_password: str = "postgres"

    # LlamaParse
    llama_cloud_api_key: str = ""

    # LLM
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o-mini"
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"

    # Embeddings
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536
    embedding_api_key: str = ""
    embedding_base_url: str = "https://api.openai.com/v1"

    # Ingestion
    storage_dir: str = "/data/storage"
    max_upload_mb: int = 50
    allowed_mime_types: str = "application/pdf,image/jpeg,image/jpg,image/png"
    chunk_size: int = 1024
    chunk_overlap: int = 128

    # Query
    retrieval_top_k: int = 6
    retrieval_min_score: float = 0.0

    # Auth
    service_api_key: str = ""

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def allowed_mime_set(self) -> set[str]:
        return {m.strip().lower() for m in self.allowed_mime_types.split(",") if m.strip()}


@lru_cache
def _get() -> Settings:
    return Settings()


settings = _get()
