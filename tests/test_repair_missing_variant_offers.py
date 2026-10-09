import pytest
from scripts.repair_missing_variant_offers import validate_target


@pytest.mark.parametrize(
    "env,url",
    [
        ("production", "postgresql://operator@10.122.0.3/pivota"),
        ("staging", "postgresql://operator@10.122.0.4/pivota"),
        ("staging", "postgresql://operator@10.122.0.3/production"),
        ("staging", "postgresql://operator@10.122.0.3/pivota?host="),
        ("staging", "postgresql://operator@10.122.0.3/pivota?options=-c+search_path=other"),
    ],
)
def test_refuses_other_environment_database_or_hidden_target_override(monkeypatch, env, url):
    monkeypatch.setenv("PIVOTA_ENV", env)
    monkeypatch.setenv("DATABASE_URL", url)
    with pytest.raises(RuntimeError):
        validate_target()


def test_only_expected_staging_target_accepted(monkeypatch):
    monkeypatch.setenv("PIVOTA_ENV", "staging")
    monkeypatch.setenv("DATABASE_URL", "postgresql://operator@10.122.0.3/pivota")
    validate_target()


def test_production_requires_explicit_environment_and_current_database(monkeypatch):
    monkeypatch.setenv('PIVOTA_ENV','production')
    monkeypatch.setenv('DATABASE_URL','postgresql://operator@10.25.0.2/pivota_08220842_Zsqlgz')
    with pytest.raises(RuntimeError):validate_target()
    validate_target('production')
    monkeypatch.setenv('DATABASE_URL','postgresql://operator@10.25.0.2/pivota')
    with pytest.raises(RuntimeError):validate_target('production')
