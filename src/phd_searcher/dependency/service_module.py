from injector import Binder, Module

from phd_searcher.service.catalog_service import CatalogService
from phd_searcher.service.export_service import ExportService
from phd_searcher.service.feedback_service import FeedbackService
from phd_searcher.service.macro_service import MacroService
from phd_searcher.service.schedule_service import ScheduleService
from phd_searcher.service.search_service import SearchService


class ServiceModule(Module):
    def configure(self, binder: Binder) -> None:
        binder.bind(SearchService)
        binder.bind(CatalogService)
        binder.bind(ExportService)
        binder.bind(FeedbackService)
        binder.bind(MacroService)
        binder.bind(ScheduleService)
