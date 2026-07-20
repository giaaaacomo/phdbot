from injector import Injector

from phd_searcher.dependency.ai_module import AIModule
from phd_searcher.dependency.config_module import ConfigModule
from phd_searcher.dependency.database_module import DatabaseModule
from phd_searcher.dependency.qdrant_module import QdrantModule
from phd_searcher.dependency.service_module import ServiceModule

container = Injector([ConfigModule(), AIModule(), DatabaseModule(), QdrantModule(), ServiceModule()])
