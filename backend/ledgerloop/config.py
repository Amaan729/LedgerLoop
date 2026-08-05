import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str
    redis_url: str
    stream_key: str
    consumer_group: str
    batch_size: int
    anthropic_api_key: str | None


def load_settings() -> Settings:
    return Settings(
        database_url=os.getenv("LEDGERLOOP_DATABASE_URL", "sqlite:///ledgerloop.db"),
        redis_url=os.getenv("LEDGERLOOP_REDIS_URL", "redis://localhost:6379/0"),
        stream_key=os.getenv("LEDGERLOOP_STREAM", "ledgerloop:inbound"),
        consumer_group=os.getenv("LEDGERLOOP_GROUP", "engine"),
        batch_size=int(os.getenv("LEDGERLOOP_BATCH_SIZE", "500")),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
    )
