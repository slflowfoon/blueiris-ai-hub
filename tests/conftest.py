import importlib
import os
import tempfile

import redis
import pytest

os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")

_test_redis = redis.from_url(os.environ["REDIS_URL"])


def _test_app_modules():
    wsgi = importlib.import_module("wsgi")
    settings_store = importlib.import_module("settings_store")
    return wsgi, settings_store


@pytest.fixture(autouse=True)
def isolate_redis_db():
    _test_redis.flushdb()
    yield
    _test_redis.flushdb()

@pytest.fixture
def client():
    """Configures the app for testing with a temporary database."""
    wsgi, settings_store = _test_app_modules()
    db_fd, db_path = tempfile.mkstemp()

    wsgi.app.config["TESTING"] = True
    wsgi.DB_FILE = db_path
    wsgi.LOG_FILE = os.path.join(os.path.dirname(db_path), "test.log")
    settings_store.DB_FILE = db_path

    with wsgi.app.test_client() as client:
        with wsgi.app.app_context():
            wsgi.init_db()
        yield client

    os.close(db_fd)
    os.unlink(db_path)
