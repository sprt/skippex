from typing import Dict, List, MutableMapping, Tuple, Union

from uuid import uuid4


# Only allow storing built-in types because other objects are tricky to
# (de)serialize when their structure changes.
DatabaseValue = Union[
    int, float, str,
    Tuple['DatabaseValue'],
    List['DatabaseValue'],
    Dict[str, 'DatabaseValue'],
]

DatabaseStore = MutableMapping[str, DatabaseValue]


class Database:
    def __init__(self, store: DatabaseStore):
        self._store = store

    @property
    def app_id(self) -> str:
        default = str(uuid4())
        identifier = self._store.setdefault('app_id', default)
        return str(identifier)

    @property
    def auth_token(self) -> str:
        return str(self._store['auth_token'])

    @auth_token.setter
    def auth_token(self, value: str):
        self._store['auth_token'] = value

    def content(self) -> DatabaseStore:
        return dict(self._store.items())
