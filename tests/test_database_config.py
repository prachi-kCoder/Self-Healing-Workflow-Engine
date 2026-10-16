from sqlalchemy.engine import make_url

from backend.db import database_url_from_environment


def test_explicit_database_url_takes_precedence(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./explicit.db")
    monkeypatch.setenv("DB_HOST", "database.internal")
    monkeypatch.setenv("DB_PASSWORD", "unused")
    assert database_url_from_environment() == "sqlite+aiosqlite:///./explicit.db"


def test_aws_database_environment_builds_escaped_async_postgres_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DB_HOST", "database.internal")
    monkeypatch.setenv("DB_PORT", "5432")
    monkeypatch.setenv("DB_NAME", "workflow")
    monkeypatch.setenv("DB_USER", "workflow")
    monkeypatch.setenv("DB_PASSWORD", "p@ss:/word#with?symbols")

    url = make_url(database_url_from_environment())

    assert url.drivername == "postgresql+asyncpg"
    assert url.host == "database.internal"
    assert url.database == "workflow"
    assert url.password == "p@ss:/word#with?symbols"


def test_sqlite_is_the_local_default_when_no_database_settings_exist(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("DB_HOST", raising=False)
    assert database_url_from_environment() == "sqlite+aiosqlite:///./workflow.db"
