from __future__ import annotations

import os
import shutil
import tempfile
from typing import Generator
import pytest
from starlette.testclient import TestClient

# Configure environment for testing prior to module imports
os.environ["APP_ENV"] = "testing"
os.environ["RATE_LIMIT_ENABLED"] = "true"
os.environ["API_KEY_REQUIRED"] = "false"
os.environ["COG_STORAGE_BACKEND"] = "local"
os.environ["STORM_DB_PATH"] = os.path.join(tempfile.mkdtemp(prefix="storm_test_db_"), "storms.db")

from config import Config
from main import app
from services import cog_storage


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment() -> Generator[None, None, None]:
    temp_dir = tempfile.mkdtemp(prefix="storm_test_cogs_")
    Config.COG_STORAGE_DIR = temp_dir
    cog_storage.local_root = type(cog_storage.local_root)(temp_dir)
    cog_storage.backend = "local"

    yield

    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def client() -> Generator[TestClient, None, None]:
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c

