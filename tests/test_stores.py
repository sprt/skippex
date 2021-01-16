from pathlib import Path
import shelve

import pytest

from skippex.stores import Database


@pytest.fixture
def db(request, tmp_path: Path) -> Database:
    if request.param == 'dict':
        return Database({})
    elif request.param == 'shelf':
        shelf = shelve.open(str(tmp_path / 'shelf.db'))
        return Database(shelf)
    else:
        raise ValueError


class TestDatabase:
    @pytest.mark.parametrize('db', ['dict', 'shelf'], indirect=True)
    def test_app_id__has_default(self, db: Database):
        assert db.app_id

    @pytest.mark.parametrize('db', ['dict', 'shelf'], indirect=True)
    def test_app_id__has_default(self, db: Database):
        assert db.app_id

    @pytest.mark.parametrize('db', ['dict', 'shelf'], indirect=True)
    def test_app_id__default_persists(self, db: Database):
        default = db.app_id
        assert db.app_id == default

    def test_app_id__default_is_unique(self):
        db1 = Database({})
        db2 = Database({})
        assert db1.app_id != db2.app_id
