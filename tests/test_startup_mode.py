from utils.startup_mode import should_skip_heavy_startup


def test_should_not_skip_heavy_startup_locally_by_default():
    assert should_skip_heavy_startup({}) is False


def test_should_skip_heavy_startup_on_railway_staging_by_default():
    assert should_skip_heavy_startup({"RAILWAY_ENVIRONMENT": "staging"}) is True


def test_should_skip_heavy_startup_on_railway_production_by_default():
    assert should_skip_heavy_startup({"RAILWAY_ENVIRONMENT": "production"}) is True


def test_explicit_false_disables_skip_even_on_railway():
    assert (
        should_skip_heavy_startup(
            {
                "RAILWAY_ENVIRONMENT": "staging",
                "SKIP_HEAVY_STARTUP_INIT": "false",
            }
        )
        is False
    )


def test_explicit_true_enables_skip_even_off_railway():
    assert should_skip_heavy_startup({"SKIP_HEAVY_STARTUP_INIT": "true"}) is True
