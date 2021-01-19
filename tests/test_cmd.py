import argparse

import pytest

from skippex.auth import PlexApplication
from skippex.cmd import EXIT_UNAUTHORIZED, cmd_run
from skippex.stores import Database


@pytest.fixture
def args() -> argparse.Namespace:
    return argparse.Namespace()


@pytest.fixture
def db() -> Database:
    return Database({})


@pytest.fixture
def app() -> PlexApplication:
    return PlexApplication(name='dummy', identifier='dummy_id')


def test_cmd_run__empty_db_returns_exit_unauthorized(
    args: argparse.Namespace,
    db: Database,
    app: PlexApplication
):
    assert cmd_run(args, db, app) == EXIT_UNAUTHORIZED
