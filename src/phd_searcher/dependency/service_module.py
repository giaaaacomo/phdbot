from injector import Binder, Module

from phd_searcher.service.catalog_service import CatalogService
from phd_searcher.service.search_service import SearchService


class ServiceModule(Module):
    def configure(self, binder: Binder) -> None:
        binder.bind(SearchService)
        binder.bind(CatalogService)
