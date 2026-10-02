import pytest
from pydantic import ValidationError

from tests.support import make_settings


def test_valid_settings() -> None:
    settings = make_settings(cors_origins="http://a.test, http://b.test,")
    assert settings.api_port == 8100
    assert settings.cors_origin_list == ["http://a.test", "http://b.test"]
    assert not settings.is_production


def test_production_refuses_the_development_admin_token() -> None:
    with pytest.raises(ValidationError, match="ADMIN_DEV_TOKEN is for development only"):
        make_settings(app_env="production")


def test_production_without_the_token_is_valid() -> None:
    assert make_settings(app_env="production", admin_dev_token=None).is_production


def test_test_database_name_must_end_in_test() -> None:
    with pytest.raises(ValidationError, match="must point to a database whose name ends in _test"):
        make_settings(database_url_test="postgresql+psycopg://u:p@h:1/fragancia")


def test_only_the_psycopg_driver_is_accepted() -> None:
    with pytest.raises(ValidationError, match="postgresql\\+psycopg"):
        make_settings(database_url="postgresql://u:p@h:1/fragancia")


def test_for_tests_points_at_the_test_database() -> None:
    settings = make_settings().for_tests()
    assert settings.database_url.endswith("/fragancia_test")
    assert settings.app_env == "test"


def test_for_tests_requires_a_test_database() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL_TEST is required"):
        make_settings(database_url_test=None).for_tests()
