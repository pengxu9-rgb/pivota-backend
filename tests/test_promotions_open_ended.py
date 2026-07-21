from datetime import datetime, timedelta


from services.promotions_service import PromotionStatus, PromotionUpdate, _compute_status


def test_open_ended_promotion_status_is_active():
    assert (
        _compute_status(
            {
                "deleted_at": None,
                "start_at": datetime.utcnow() - timedelta(days=1),
                "end_at": None,
            }
        )
        == PromotionStatus.ACTIVE
    )


def test_promotion_update_tracks_explicit_null_end_at():
    update = PromotionUpdate(endAt=None)
    fields_set = getattr(update, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(update, "__fields_set__", set())

    assert "endAt" in fields_set
