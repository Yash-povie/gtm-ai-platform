from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Core runtime
    gtm_env: Literal["sandbox", "production"] = "sandbox"
    gtm_live_send_enabled: bool = False
    gtm_require_approval_all: bool = True
    log_level: str = "INFO"

    # Datastores
    database_url: str = "postgresql+asyncpg://gtm:gtm_pass@localhost:5432/gtm_platform"
    redis_url: str = "redis://localhost:6379/0"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "neo4j_pass"

    # LLMs & embeddings
    anthropic_api_key: str = "mock"
    llm_primary_model: str = "claude-sonnet-5-5"
    llm_judge_model: str = "claude-haiku-5-5"
    embedding_provider: Literal["local_fastembed", "openai", "hash"] = "hash"
    embedding_dim: int = 1536

    # Integrations ("mock" => deterministic sandbox fixtures)
    apollo_api_key: str = "mock"
    clay_webhook_url: str = "http://localhost:8000/webhooks/mock-clay"
    clay_signing_secret: str = "local_clay_secret"
    hubspot_private_app_token: str = "mock"
    salesforce_client_id: str = "mock"
    salesforce_client_secret: str = "mock"
    salesforce_username: str = "mock"
    salesforce_password: str = "mock"
    salesforce_security_token: str = "mock"
    n8n_webhook_secret: str = "local_n8n_secret"
    slack_bot_token: str = "xoxb-mock"
    slack_signing_secret: str = "mock_slack_secret"
    slack_approval_channel_id: str = "C00000000"

    # Compliance
    unsubscribe_jwt_secret: str = "local_unsub_secret_change_me_32bytes!"
    unsubscribe_base_url: str = "http://localhost:8000"
    sender_postal_address: str = "123 Example St, Suite 100, San Francisco, CA 94105, USA"

    # Safety limits
    global_daily_send_cap: int = 50
    domain_daily_send_cap: int = 3
    contact_cooldown_days: int = 14
    approval_ttl_hours: int = 48

    # Observability
    phoenix_collector_endpoint: str = "http://localhost:4317"

    @property
    def is_sandbox(self) -> bool:
        return self.gtm_env == "sandbox"


@lru_cache
def get_settings() -> Settings:
    return Settings()
