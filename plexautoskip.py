from __future__ import annotations

import argparse
import configparser
from functools import partial
from pathlib import Path
from time import sleep
from typing import Any, Iterable, NamedTuple, Optional
from urllib.parse import urlencode
import uuid
import sys
import webbrowser

from plexapi.myplex import PlexServer
import requests

_APP_NAME = 'Plex Auto-Skip'
_PLEXAUTOSKIP_INI = Path('.plexautoskip.ini')


def _print_stderr(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


class INIReadWriter(configparser.ConfigParser):
    def __init__(self, filename: Path, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._filename = filename
        self._filename.touch(exist_ok=True)
        super().read_file(open(self._filename, 'r+'))

    def read_file(self, f: Iterable[str], source: Optional[str]):
        raise NotImplementedError

    # TODO: prohibit other read_* methods

    def close(self):
        with open(self._filename, 'r+') as f:
            self.write(f)


class Database:
    def __init__(self, ini_rw: INIReadWriter):
        try:
            ini_rw.add_section('database')
        except configparser.DuplicateSectionError:
            pass
        self._ini_rw = ini_rw
        self._db = ini_rw['database']

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._ini_rw.close()

    @property
    def app_id(self):
        return self._db.setdefault('app_id', str(uuid.uuid4()))

    @property
    def auth_token(self) -> str:
        return self._db['auth_token']

    @auth_token.setter
    def auth_token(self, value: str):
        self._db['auth_token'] = value


class PlexApplication(NamedTuple):
    name: str
    identifier: str


class PlexAuthClient:
    # Reference: https://forums.plex.tv/t/authenticating-with-plex/609370

    _BASE_API_URL = 'https://plex.tv/api/v2'
    _BASE_AUTH_URL = 'https://app.plex.tv/auth'

    def __init__(self, app: PlexApplication):
        self._app = app

    def _make_request(self, method: str, endpoint: str, **kwargs: dict[str, Any]) -> requests.Response:
        url = self._BASE_API_URL + endpoint
        return requests.request(method, url, **kwargs)

    def is_token_valid(self, token: str) -> bool:
        headers = {'Accept': 'application/json'}
        data = {
            'X-Plex-Product': self._app.name,
            'X-Plex-Client-Identifier': self._app.identifier,
            'X-Plex-Token': token,
        }
        r = self._make_request('GET', '/user', headers=headers, data=data)

        if r.status_code == 200:
            return True
        elif r.status_code == 401:
            return False
        else:
            r.raise_for_status()

    def generate_pin(self) -> tuple[int, str]:
        headers = {'Accept': 'application/json'}
        data = {
            'strong': 'true',
            'X-Plex-Product': self._app.name,
            'X-Plex-Client-Identifier': self._app.identifier,
        }
        r = self._make_request('POST', '/pins', headers=headers, data=data)
        r.raise_for_status()
        info = r.json()
        return info['id'], info['code']

    def generate_auth_url(self, pin_code: str) -> str:
        qs = urlencode({
            'clientID': self._app.identifier,
            'code': pin_code,
            'context[device][product]': self._app.name,
        })
        return self._BASE_AUTH_URL + '#?' + qs

    def check_pin(self, pin_id: int, pin_code: str) -> Optional[str]:
        headers = {'Accept': 'application/json'}
        data = {
            'code': pin_code,
            'X-Plex-Client-Identifier': self._app.identifier,
        }
        r = self._make_request('GET', f'/pins/{pin_id}', headers=headers, data=data)
        r.raise_for_status()
        info = r.json()
        return info['authToken']

    def wait_for_token(self, pin_id: int, pin_code: int, check_interval_sec=1) -> str:
        auth_token = self.check_pin(pin_id, pin_code)
        while not auth_token:
            sleep(check_interval_sec)
            auth_token = self.check_pin(pin_id, pin_code)
        return auth_token


def cmd_auth(args: argparse.Namespace, db: Database):
    app = PlexApplication(name=_APP_NAME, identifier=db.app_id)
    plex_auth = PlexAuthClient(app)
    pin_id, pin_code = plex_auth.generate_pin()
    auth_url = plex_auth.generate_auth_url(pin_code)

    webbrowser.open_new_tab(auth_url)
    _print_stderr('Navigate to the following page to authorize this application:')
    _print_stderr(auth_url)
    _print_stderr()

    _print_stderr('Waiting for successful authorization...')
    auth_token = plex_auth.wait_for_token(pin_id, pin_code)

    db.auth_token = auth_token
    _print_stderr('Authorization successful')


def cmd_default(args: argparse.Namespace, db: Database):
    raise NotImplementedError


def main():
    with Database(INIReadWriter(_PLEXAUTOSKIP_INI)) as db:
        parser = argparse.ArgumentParser()
        parser.set_defaults(func=partial(cmd_default, db=db))

        subparsers = parser.add_subparsers(title='subcommands')
        parser_auth = subparsers.add_parser('auth', help='authorize the application to access your Plex Account')
        parser_auth.set_defaults(func=partial(cmd_auth, db=db))

        args = parser.parse_args()
        sys.exit(args.func(args))


if __name__ == '__main__':
    main()
