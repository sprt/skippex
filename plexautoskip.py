from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator, MutableMapping
import configparser
from functools import partial
from io import TextIOBase
import json
from pathlib import Path
from time import sleep
from typing import Any, Literal, NamedTuple, Optional, TypedDict
from urllib.parse import urlencode
import uuid
import sys
import webbrowser

from plexapi.alert import AlertListener as PlexAlertListener
from plexapi.client import PlexClient
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer
from plexapi.video import Episode
import requests

_APP_NAME = 'Plex Auto-Skip'
_PLEXAUTOSKIP_INI = Path('.plexautoskip.ini')


def _print_stderr(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


Store = MutableMapping[str, Any]


class INIStore(Store):
    # TODO: Use a file lock (portalocker)

    def __init__(self, get_fp: Callable[[], TextIOBase], section: str):
        self._get_fp = get_fp
        self._section = section

    def _read(self) -> configparser.ConfigParser:
        cp = configparser.ConfigParser()
        cp.read_file(self._get_fp())
        try:
            cp.add_section(self._section)
        except configparser.DuplicateSectionError:
            pass
        return cp

    def _write(self, cp: configparser.ConfigParser):
        cp.write(self._get_fp())

    def __getitem__(self, k: str) -> Any:
        cp = self._read()
        return cp[self._section][k]

    def __setitem__(self, k: str, v: Any):
        cp = self._read()
        cp[self._section][k] = v
        self._write(cp)

    def __delitem__(self, k: str):
        cp = self._read()
        del cp[self._section][k]
        self._write(cp)

    def __iter__(self) -> Iterator:
        cp = self._read()
        return iter(cp[self._section])

    def __len__(self) -> int:
        cp = self._read()
        return len(cp[self._section])


class Database:
    def __init__(self, store: Store):
        self._store = store

    @property
    def app_id(self):
        return self._store.setdefault('app_id', str(uuid.uuid4()))

    @property
    def auth_token(self) -> str:
        return self._store['auth_token']

    @auth_token.setter
    def auth_token(self, value: str):
        self._store['auth_token'] = value


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
        return info['authToken'] if info['authToken'] else None

    def wait_for_token(self, pin_id: int, pin_code: int, check_interval_sec=1) -> str:
        auth_token = self.check_pin(pin_id, pin_code)
        while not auth_token:
            sleep(check_interval_sec)
            auth_token = self.check_pin(pin_id, pin_code)
        return auth_token


def cmd_auth(args: argparse.Namespace, db: Database, app: PlexApplication):
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


class AlertListener(PlexAlertListener):
    def _onMessage(self, *args):
        # The upstream implementation silently logs exceptions when they happen,
        # and never raises them. We don't want that.
        message = args[-1]
        data = json.loads(message)['NotificationContainer']
        if self._callback:
            self._callback(data)


class PlaybackNotification(TypedDict):
    sessionKey: str  # Not an int!
    guid: str  # Can be the empty string
    ratingKey: str
    url: str  # Can be the empty string
    key: str
    viewOffset: int  # In milliseconds
    playQueueItemID: int
    state: Literal['buffering', 'playing', 'paused']


class NoMatchingClientError(Exception):
    """Raised when a session (episode being watched) has no matching client."""


class NoMatchingSessionError(Exception):
    """Raised when a notification has no matching session."""


class AlertCallback:
    def __init__(self, server: PlexServer, account: MyPlexAccount, db: Database):
        self._server = server
        self._account = account
        self._db = db

    def _match_session(self, notification: PlaybackNotification) -> Episode:
        for session in self._server.sessions():
            is_match = (
                str(session.sessionKey) == notification['sessionKey']
                and isinstance(session, Episode)
                and session.hasIntroMarker
            )
            if is_match:
                return session
        raise NoMatchingSessionError

    def _match_client(self, session: Episode) -> PlexClient:
        # Search server.clients() using machineIdentifier. session.players()
        # doesn't work because the PlexClients don't have a baseurl, thus
        # can't make requests.

        # XXX: Does the session ever not contain exactly 1 player?
        sess_machine_id = session.players[0].machineIdentifier
        # NOTE: Have to "advertize as player" in order to be seen by clients().
        for client in self._server.clients():
            if client.machineIdentifier == sess_machine_id:
                return client
        raise NoMatchingClientError

    def _handle_notification(self, notification: PlaybackNotification):
        if notification['state'] != 'playing':
            return

        try:
            session = self._match_session(notification)
        except NoMatchingSessionError:
            # That's okay. Could simply be that the user is watching a movie,
            # or that the episode has no intro markers.
            return

        client = self._match_client(session)
        view_offset = notification['viewOffset']
        intro_marker = next(m for m in session.markers if m.type == 'intro')

        print(f'{notification["sessionKey"]=} {notification["viewOffset"]=} {notification["state"]=}')
        print(f'{session=}')
        print(f'{client=}')
        print(f'{intro_marker=}')

        if intro_marker.start <= view_offset and view_offset < intro_marker.end:
            client.seekTo(intro_marker.end)
            print(f'Seeked from {view_offset} to {intro_marker.end}')
        else:
            print('Did not seek')

        print()

    def __call__(self, alert: dict[str, Any]):
        if alert['type'] == 'playing':
            # Never seen a case where the alert doesn't contain exactly
            # 1 notification, but let's loop over the list out of caution.
            for notification in alert['PlaySessionStateNotification']:
                self._handle_notification(notification)


def cmd_default(args: argparse.Namespace, db: Database, app: PlexApplication) -> Optional[int]:
    try:
        auth_token = db.auth_token
    except KeyError:
        _print_stderr(
            "No credentials found. Please first run the 'auth' command to "
            "authorize the application."
        )
        return 1

    auth_client = PlexAuthClient(app)
    if not auth_client.is_token_valid(auth_token):
        _print_stderr("Token invalid. Please run the 'auth' command to reauthenticate yourself.")
        return 1

    account = MyPlexAccount(token=auth_token)

    try:
        server_resource = next(r for r in account.resources() if 'server' in r.provides)
    except StopIteration:
        _print_stderr("Couldn't find a Plex server for this account.")
        return 1

    # TODO: Ensure we try HTTP only if HTTPS fails.
    server = server_resource.connect()

    alert_callback = AlertCallback(server, account, db)
    alert_listener = AlertListener(server, alert_callback)
    alert_listener.start()
    alert_listener.join()


def main():
    _PLEXAUTOSKIP_INI.touch()
    f = lambda: open(_PLEXAUTOSKIP_INI, 'r+')
    ini_store = INIStore(f, section='database')
    db = Database(ini_store)
    app = PlexApplication(name=_APP_NAME, identifier=db.app_id)

    parser = argparse.ArgumentParser()
    parser.set_defaults(func=partial(cmd_default, db=db, app=app))

    subparsers = parser.add_subparsers(title='subcommands')
    parser_auth = subparsers.add_parser('auth', help='authorize this application to access your Plex account')
    parser_auth.set_defaults(func=partial(cmd_auth, db=db, app=app))

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == '__main__':
    main()
