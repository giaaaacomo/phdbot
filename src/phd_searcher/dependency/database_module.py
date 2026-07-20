from injector import Module, provider, singleton
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from phd_searcher.config.database import DatabaseConfig


class DatabaseModule(Module):
    @singleton
    @provider
    def provide_async_engine(self, config: DatabaseConfig) -> AsyncEngine:
        return create_async_engine(
            config.url,
            echo=False,
            future=True,
            poolclass=NullPool,
            execution_options={"schema_translate_map": {None: config.schema_name}},
        )

    @singleton
    @provider
    def provide_session_maker(self, engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
        return async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
