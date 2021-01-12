from typing import Tuple

import pytest

from skippex.stores import Database


@pytest.fixture
def db() -> Database:
    return Database({})


@pytest.fixture
def two_dbs() -> Tuple[Database, Database]:
    return Database({}), Database({})


class TestDatabase:
    def test_app_id__has_default(self, db: Database):
        assert db.app_id

    def test_app_id__default_persists(self, db: Database):
        default = db.app_id
        assert db.app_id == default

    def test_app_id__default_is_unique(self, two_dbs: Tuple[Database, Database]):
        db1, db2 = two_dbs
        assert db1.app_id != db2.app_id
