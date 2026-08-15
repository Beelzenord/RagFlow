from functools import lru_cache
from typing import Any, Literal
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # Postgres
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "postgres"
    postgres_user: str = "postgres"
    postgres_password: str = "postgres"
    # Flexible Server requires TLS. Local compose Postgres does not, so the
    # default is "prefer": no SSL unless the server offers it in a way the
    # driver already handles. Set POSTGRES_SSL=require on Azure.
    postgres_ssl: Literal["disable", "prefer", "require"] = "prefer"

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
    chunk_size: int = 512
    chunk_overlap: int = 64

    # Parse verification. Thresholds are calibrated so that a page has to
    # collapse, not merely be tidied up, before it counts as a failure - see
    # ingestion/app/quality.py for what each number is measuring.
    parse_verify_enabled: bool = True
    parse_min_coverage: float = 0.35
    parse_min_word_length: float = 3.2
    parse_max_single_char_ratio: float = 0.35
    # Re-parsing in premium mode costs materially more, so cap how many pages
    # of one document can trigger it before we fall back to the text layer.
    parse_repair_max_pages: int = 20

    # Query. Each retriever contributes retrieval_pool_size candidates, which
    # are fused and cut to retrieval_top_k. min_score gates the vector side
    # only - lexical hits are already filtered by matching the query terms.
    retrieval_top_k: int = 6
    retrieval_min_score: float = 0.25
    retrieval_pool_size: int = 30
    # Reciprocal Rank Fusion constant. Higher flattens the weighting between
    # ranks; 60 is the value from the original RRF paper and a sane default.
    retrieval_rrf_k: int = 60
    # Lexical search ignores any term appearing in more than this share of
    # chunks. It is there to find identifiers, not to re-do semantic search.
    retrieval_lexical_max_df: float = 0.02

    # Auth
    service_api_key: str = ""

    @property
    def database_url(self) -> str:
        # Azure-generated passwords contain @, #, etc.; an unquoted URL
        # treats the first @ as the host separator and the connection dies.
        user = quote_plus(self.postgres_user)
        password = quote_plus(self.postgres_password)
        return (
            f"postgresql+asyncpg://{user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def database_connect_args(self) -> dict[str, Any]:
        if self.postgres_ssl == "require":
            return {"ssl": True}
        if self.postgres_ssl == "disable":
            return {"ssl": False}
        return {}

    @property
    def allowed_mime_set(self) -> set[str]:
        return {m.strip().lower() for m in self.allowed_mime_types.split(",") if m.strip()}


@lru_cache
def _get() -> Settings:
    return Settings()


settings = _get()
