import pytest

from app.config import Settings


def test_development_defaults_are_usable() -> None:
    settings = Settings(_env_file=None)
    assert settings.environment == "development"
    assert settings.secret_key  # a throwaway key is generated for development
    assert settings.database_url.startswith("sqlite")


def test_production_requires_an_explicit_secret_key() -> None:
    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(_env_file=None, environment="production")


def test_production_accepts_an_explicit_secret_key() -> None:
    settings = Settings(
        _env_file=None,
        environment="production",
        secret_key="x" * 32,
        base_url="https://example.org",
    )
    assert settings.is_production is True
    assert settings.secret_key == "x" * 32
