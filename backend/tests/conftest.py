import os

import pytest

from ledgerloop import db


@pytest.fixture
def engine():
    """SQLite in memory by default. Set LEDGERLOOP_TEST_DATABASE_URL to run against Postgres."""
    url = os.getenv("LEDGERLOOP_TEST_DATABASE_URL", "sqlite+pysqlite:///:memory:")
    eng = db.make_engine(url)
    db.reset_db(eng)
    yield eng
    eng.dispose()
