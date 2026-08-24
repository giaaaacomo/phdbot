# Import ORM models here so Alembic autogenerate sees them (registration only).
from phd_searcher.database.models.listing_page import ListingPage  # noqa: F401
from phd_searcher.database.models.macro import MacroRun, SavedMacro  # noqa: F401
from phd_searcher.database.models.pipeline_run import PipelineRun  # noqa: F401
from phd_searcher.database.models.position import Position  # noqa: F401
from phd_searcher.database.models.position_feedback import PositionFeedback  # noqa: F401
from phd_searcher.database.models.review_attempt import ReviewAttempt  # noqa: F401
from phd_searcher.database.models.scheduled_job import ScheduledJob  # noqa: F401
from phd_searcher.database.models.university import University  # noqa: F401
