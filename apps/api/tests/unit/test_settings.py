import pytest
from pydantic import ValidationError

from tests.support import make_settings

PROD_SECRET = "p" * 32
PROD_MP = {"mercadopago_access_token": "APP_USR-test", "mercadopago_sandbox": False}


def test_valid_settings() -> None:
    settings = make_settings(cors_origins="http://a.test, http://b.test,")
    assert settings.api_port == 8100
    assert settings.cors_origin_list == ["http://a.test", "http://b.test"]
    assert not settings.is_production


def test_production_settings_are_valid() -> None:
    assert make_settings(
        app_env="production", order_link_secret=PROD_SECRET, **PROD_MP
    ).is_production


@pytest.mark.parametrize("secret", [None, "", "s" * 31])
def test_production_requires_an_order_link_secret_of_32_characters(secret: str | None) -> None:
    with pytest.raises(ValidationError, match="ORDER_LINK_SECRET must have at least 32"):
        make_settings(app_env="production", order_link_secret=secret, **PROD_MP)


def test_a_32_character_order_link_secret_is_accepted_in_production() -> None:
    assert make_settings(app_env="production", order_link_secret="s" * 32, **PROD_MP).is_production


def test_the_order_link_secret_is_optional_outside_production() -> None:
    assert make_settings().order_link_secret is None
    assert make_settings(app_env="test").order_link_secret is None


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


def test_account_email_defaults() -> None:
    settings = make_settings()
    assert (settings.invitation_ttl_hours, settings.password_reset_ttl_minutes) == (72, 60)
    assert settings.admin_web_url == "http://localhost:4200"
    assert (settings.smtp_host, settings.smtp_port) == ("127.0.0.1", 1026)
    assert (settings.smtp_username, settings.smtp_password) == (None, None)
    assert settings.smtp_starttls is False
    assert settings.mail_from == "La Fragancia Ideal <no-reply@lafraganciaideal.test>"


@pytest.mark.parametrize(
    "url", ["localhost:4200", "ftp://x", "//x", "http://x/", "https://admin.example.test/", ""]
)
def test_admin_web_url_needs_a_scheme_and_no_trailing_slash(url: str) -> None:
    with pytest.raises(ValidationError, match="ADMIN_WEB_URL must start with http"):
        make_settings(admin_web_url=url)


@pytest.mark.parametrize("url", ["http://x", "https://admin.example.test", "http://localhost:4200"])
def test_admin_web_url_accepts_a_base_url(url: str) -> None:
    assert make_settings(admin_web_url=url).admin_web_url == url


@pytest.mark.parametrize(
    "url", ["localhost:4200", "ftp://x", "//x", "http://x/", "https://tienda.example.test/", ""]
)
def test_storefront_url_needs_a_scheme_and_no_trailing_slash(url: str) -> None:
    with pytest.raises(ValidationError, match="STOREFRONT_URL must start with http"):
        make_settings(storefront_url=url)


@pytest.mark.parametrize(
    "url", ["http://x", "https://tienda.example.test", "http://localhost:4200"]
)
def test_storefront_url_accepts_a_base_url(url: str) -> None:
    assert make_settings(storefront_url=url).storefront_url == url


def test_storefront_url_defaults_to_the_local_storefront() -> None:
    assert make_settings().storefront_url == "http://localhost:4200"


@pytest.mark.parametrize(
    "override",
    [
        {"invitation_ttl_hours": 0},
        {"password_reset_ttl_minutes": 0},
        {"smtp_port": 0},
        {"smtp_port": 65536},
    ],
)
def test_account_email_numbers_are_bounded(override: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        make_settings(**override)


def test_the_smtp_password_is_a_secret_that_does_not_show_in_the_repr() -> None:
    settings = make_settings(smtp_password="s3cret-value")
    assert settings.smtp_password is not None
    assert settings.smtp_password.get_secret_value() == "s3cret-value"
    assert "s3cret-value" not in repr(settings)


def test_media_storage_defaults_point_at_the_local_rustfs() -> None:
    settings = make_settings()

    assert settings.s3_endpoint_url == "http://127.0.0.1:9100"
    assert (settings.s3_access_key, settings.s3_secret_key) == (None, None)
    assert (settings.s3_bucket, settings.s3_region) == ("fragancia-media", "us-east-1")
    assert settings.media_public_base_url == "http://127.0.0.1:9100/fragancia-media"
    assert settings.photo_max_bytes == 10_485_760


@pytest.mark.parametrize(
    "url",
    [
        "127.0.0.1:9100/fragancia-media",
        "ftp://cdn.example.test",
        "//cdn.example.test",
        "http://127.0.0.1:9100/fragancia-media/",
        "https://cdn.example.test/",
        "",
    ],
)
def test_media_public_base_url_needs_a_scheme_and_no_trailing_slash(url: str) -> None:
    with pytest.raises(ValidationError, match="MEDIA_PUBLIC_BASE_URL must start with http"):
        make_settings(media_public_base_url=url)


@pytest.mark.parametrize(
    "url",
    ["http://x", "https://cdn.example.test", "http://127.0.0.1:9100/fragancia-media"],
)
def test_media_public_base_url_accepts_a_base_url(url: str) -> None:
    assert make_settings(media_public_base_url=url).media_public_base_url == url


@pytest.mark.parametrize("value", [0, -1])
def test_photo_max_bytes_must_be_positive(value: int) -> None:
    with pytest.raises(ValidationError):
        make_settings(photo_max_bytes=value)


def test_photo_max_bytes_accepts_one() -> None:
    assert make_settings(photo_max_bytes=1).photo_max_bytes == 1


def test_the_s3_secret_key_is_a_secret_that_does_not_show_in_the_repr() -> None:
    settings = make_settings(s3_access_key="access", s3_secret_key="s3cret-key-value")

    assert settings.s3_secret_key is not None
    assert settings.s3_secret_key.get_secret_value() == "s3cret-key-value"
    assert "s3cret-key-value" not in repr(settings)


@pytest.mark.parametrize("token", [None, ""])
def test_production_requires_a_mercadopago_access_token(token: str | None) -> None:
    with pytest.raises(ValidationError, match="MERCADOPAGO_ACCESS_TOKEN is required"):
        make_settings(
            app_env="production",
            order_link_secret=PROD_SECRET,
            mercadopago_access_token=token,
            mercadopago_sandbox=False,
        )


def test_production_requires_the_mercadopago_sandbox_off() -> None:
    with pytest.raises(ValidationError, match="MERCADOPAGO_SANDBOX must be false"):
        make_settings(
            app_env="production",
            order_link_secret=PROD_SECRET,
            mercadopago_access_token="APP_USR-test",
        )


@pytest.mark.parametrize(
    "url", ["http://api.mercadopago.com", "http://127.0.0.1:9999", "api.mercadopago.com", ""]
)
def test_production_requires_an_https_mercadopago_api_url(url: str) -> None:
    with pytest.raises(ValidationError, match="MERCADOPAGO_API_URL must start with https://"):
        make_settings(
            app_env="production",
            order_link_secret=PROD_SECRET,
            mercadopago_api_url=url,
            **PROD_MP,
        )


def test_production_accepts_the_https_mercadopago_api_url() -> None:
    settings = make_settings(app_env="production", order_link_secret=PROD_SECRET, **PROD_MP)
    assert settings.mercadopago_api_url == "https://api.mercadopago.com"


def test_a_local_fake_mercadopago_api_url_is_allowed_outside_production() -> None:
    settings = make_settings(mercadopago_api_url="http://127.0.0.1:9999")
    assert settings.mercadopago_api_url == "http://127.0.0.1:9999"


def test_mercadopago_is_optional_outside_production() -> None:
    settings = make_settings()
    assert settings.mercadopago_access_token is None
    assert settings.mercadopago_sandbox is True
