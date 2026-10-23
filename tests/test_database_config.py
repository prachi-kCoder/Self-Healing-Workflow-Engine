from sqlalchemy.engine import make_url

from backend.db import database_url_from_environment, normalize_database_url


def test_explicit_database_url_takes_precedence(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///./explicit.db")
    monkeypatch.setenv("DB_HOST", "database.internal")
    monkeypatch.setenv("DB_PASSWORD", "unused")
    assert database_url_from_environment() == "sqlite+aiosqlite:///./explicit.db"


def test_standard_neon_postgres_url_uses_asyncpg_and_translates_tls_options():
    url = make_url(normalize_database_url(
        "postgresql://user:secret@ep-example.neon.tech/app?sslmode=require&channel_binding=require"
    ))

    assert url.drivername == "postgresql+asyncpg"
    assert url.host == "ep-example.neon.tech"
    assert url.password == "secret"
    assert url.query == {"ssl": "require"}


def test_postgres_scheme_alias_uses_asyncpg():
    url = make_url(normalize_database_url("postgres://user:secret@db.example/app"))

    assert url.drivername == "postgresql+asyncpg"


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
