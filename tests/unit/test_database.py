from injector import Injector
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from phd_searcher.config import Settings
from phd_searcher.config.database import DatabaseConfig
from phd_searcher.config.llm import EmbeddingConfig, LLMConfig
from phd_searcher.dependency.config_module import ConfigModule
from phd_searcher.dependency.database_module import DatabaseModule


def test_database_module_provides_engine_and_sessionmaker():
    settings = Settings(
        llm=LLMConfig(model="test/model"),
        embedding=EmbeddingConfig(model="test/embedding"),
        database=DatabaseConfig(url="postgresql+asyncpg://u:p@localhost:5432/db"),
    )
    injector = Injector([ConfigModule(settings), DatabaseModule()])
    # create_async_engine is lazy — this resolves the DI graph without connecting.
    assert isinstance(injector.get(AsyncEngine), AsyncEngine)
    assert isinstance(injector.get(async_sessionmaker[AsyncSession]), async_sessionmaker)
