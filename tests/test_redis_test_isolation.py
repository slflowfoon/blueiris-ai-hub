import os
from pathlib import Path
import redis


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_test_suite_defaults_redis_url_to_isolated_db_15():
    assert os.environ["REDIS_URL"] == "redis://localhost:6379/15"


def test_ci_uses_isolated_redis_db_for_tests():
    ci_cd = (REPO_ROOT / ".github" / "workflows" / "ci-cd.yml").read_text()

    assert "export REDIS_URL=redis://localhost:6379/15" in ci_cd


def test_conftest_flushes_the_isolated_redis_db_between_tests():
    conftest = (REPO_ROOT / "tests" / "conftest.py").read_text()

    assert "flushdb()" in conftest


def test_configured_test_redis_db_starts_empty():
    client = redis.from_url(os.environ["REDIS_URL"])

    assert client.dbsize() == 0
