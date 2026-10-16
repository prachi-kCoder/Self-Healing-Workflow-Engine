import os

test_database_url = os.getenv("WORKFLOW_TEST_DATABASE_URL", "sqlite+aiosqlite:///./workflow-test.db")
if test_database_url.startswith("postgresql"):
    database_name = test_database_url.rsplit("/", 1)[-1].split("?", 1)[0]
    if "test" not in database_name.lower():
        raise RuntimeError("WORKFLOW_TEST_DATABASE_URL must point to a dedicated database with 'test' in its name")
os.environ["DATABASE_URL"] = test_database_url
os.environ["ENVIRONMENT"] = "development"

import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from backend.app import app  # noqa: E402
from backend.db import Base, engine  # noqa: E402


@pytest_asyncio.fixture(autouse=True)
async def reset_database():
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as value:
        yield value
