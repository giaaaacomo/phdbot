from phd_searcher.database.models.listing_page import ListingPage
from phd_searcher.database.models.position import Position
from phd_searcher.database.models.review_attempt import ReviewAttempt


def test_review_cascade_models_expose_durable_routing_and_audit_fields():
    assert "review_state" in Position.__table__.columns
    assert "routing_reason" in Position.__table__.columns
    assert "quality_status" in ListingPage.__table__.columns
    assert "quality_metrics" in ListingPage.__table__.columns
    assert ReviewAttempt.__table__.columns["position_id"].foreign_keys
    assert ReviewAttempt.__table__.columns["pipeline_run_id"].foreign_keys
    assert ReviewAttempt.__table__.columns["evidence"].nullable is False
