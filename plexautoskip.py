from __future__ import annotations

from abc import ABC, abstractmethod
import argparse
from collections.abc import Callable, Iterator, MutableMapping
import configparser
from datetime import datetime, timedelta
from functools import partial
from io import TextIOBase
import json
import logging
from pathlib import Path
from time import sleep
from typing import Any, Literal, NamedTuple, Optional, TypedDict, cast
from urllib.parse import urlencode
import uuid
import sys
import webbrowser

from plexapi.alert import AlertListener as PlexAlertListener
from plexapi.base import Playable
from plexapi.client import PlexClient
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer
from plexapi.video import Episode
import requests


logger = logging.getLogger('plexautoskip')

_APP_NAME = 'Plex Auto-Skip'
_PLEXAUTOSKIP_INI = Path('.plexautoskip.ini')

SessionKey = str
Store = MutableMapping[str, Any]


def _print_stderr(*args, **kwargs):
    print(*args, file=sys.stderr, **kwargs)


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

        try:
            r.raise_for_status()
        except requests.HTTPError:
            if r.status_code == 401:
                return False
            raise
        else:
            return True

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

    def wait_for_token(self, pin_id: int, pin_code: str, check_interval_sec=1) -> str:
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


class SessionListener(ABC):
    def filter_session(self, session: Playable) -> bool:
        """See the docstrings for the callback methods."""
        return True

    @abstractmethod
    def on_session_activity(self, session: Playable):
        """Called iff filter_session(session) returns True."""
        pass

    @abstractmethod
    def on_session_removal(self, session: Playable):
        """Called iff on_session_activity(session) was called at some point."""
        pass


class Seekable(ABC):
    @abstractmethod
    def seek(self, offset_ms: int):
        pass


class SeekablePlexClient(Seekable):
    def __init__(self, client: PlexClient):
        self._client = client

    def seek(self, offset_ms: int):
        self._client.seekTo(offset_ms)


class NoSeekableFoundError(Exception):
    pass


class SeekableProvider(ABC):
    @abstractmethod
    def provide_seekable(self, session: Playable) -> Seekable:
        """Raises NoSeekableFoundError if no Seekable could be found."""
        pass


class PlexSeekableProvider(SeekableProvider):
    def __init__(self, server: PlexServer):
        self._server = server

    def provide_seekable(self, session: Playable) -> Seekable:
        sess_machine_id = session.players[0].machineIdentifier
        # NOTE: Have to "advertize as player" in order to be considered a client by Plex.
        client: PlexClient
        for client in self._server.clients():
            if client.machineIdentifier == sess_machine_id:
                return SeekablePlexClient(client)
        raise NoSeekableFoundError


class ChromecastSeekableProvider(SeekableProvider):
    pass


class AutoSkipper(SessionListener):
    def __init__(self, seekable_provider: SeekableProvider):
        super().__init__()
        self._skipped: set[SessionKey] = set()
        self._sp = seekable_provider

    def filter_session(self, session: Playable) -> bool:
        if not isinstance(session, Episode):
            # Only TV shows have intro markers, other media don't interest us.
            logger.debug('Ignored; not an episode')
            return False

        # XXX: From my testing, a session always has exactly one player.
        player: PlexClient = session.players[0]
        player_state: Literal['buffering', 'paused', 'playing'] = player.state

        if player_state != 'playing':
            logger.debug(f'Ignored; state is "{player_state}" instead of "playing"')
            return False

        # Preserve the view offset.
        # See https://github.com/pkkid/python-plexapi/issues/638.
        # Potential alernative: https://stackoverflow.com/a/1445289/407054.
        try:
            view_offset = session.viewOffset
            if not session.hasIntroMarker:
                logger.debug(f'Ignored; has no intro marker')
                return False
        finally:
            session.viewOffset = view_offset

        return True

    def on_session_activity(self, session: Playable):
        session_key = str(session.sessionKey)
        if session_key in self._skipped:
            logger.debug('Ignored; already skipped during this session')
            return

        session = cast(Episode, session)  # Safe thanks to self.filter_session().
        view_offset = session.viewOffset
        intro_marker = next(m for m in session.markers if m.type == 'intro')

        logger.debug(f'{session.sessionKey=}')
        logger.debug(f'{view_offset=}')
        logger.debug(f'{intro_marker=}')

        if intro_marker.start <= view_offset and view_offset < intro_marker.end:
            seekable = self._sp.provide_seekable(session)
            seekable.seek(intro_marker.end)
            self._skipped.add(session_key)
            logger.debug(f'Skipped; seeked from {view_offset} to {intro_marker.end}')
        else:
            logger.debug('Did not skip; not viewing intro')

        logger.debug('-----')

    def on_session_removal(self, session: Playable):
        self._skipped.discard(str(session.sessionKey))


class SessionDispatcher:
    def __init__(self, listener: SessionListener, removal_timeout_sec: int = 20):
        # Plex's session WebSocket announces every 10 second, so we
        # conservatively set the default removal_timeout_sec to 20 seconds.
        self._listener = listener
        self._removal_timeout_sec = removal_timeout_sec
        self._last_active: dict[str, tuple[datetime, Playable]] = {}

    def dispatch(self, session: Playable):
        if self._listener.filter_session(session):
            self._listener.on_session_activity(session)
            key = str(session.sessionKey)
            self._last_active[key] = (datetime.now(), session)

        # Remove inactive sessions.
        timeout_ago = datetime.now() - timedelta(seconds=self._removal_timeout_sec)
        inactive = (
            (key, session) for key, (last_active, session) in self._last_active.items()
            if last_active <= timeout_ago
        )
        for inactive_key, inactive_session in inactive:
            self._listener.on_session_removal(inactive_session)
            self._last_active.pop(inactive_key, None)


class SessionDiscovery:
    def __init__(self, server: PlexServer, dispatcher: SessionDispatcher, poll_interval_sec: int = 1):
        self._server = server
        self._dispatcher = dispatcher
        self._poll_interval_sec = poll_interval_sec

    def run(self):
        while True:
            session: Playable
            for session in self._server.sessions():
                self._dispatcher.dispatch(session)
            sleep(self._poll_interval_sec)


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

    server = server_resource.connect()  # TODO: Ensure we try HTTP only if HTTPS fails.
    sp = PlexSeekableProvider(server)
    listener = AutoSkipper(seekable_provider=sp)
    dispatcher = SessionDispatcher(listener)

    discovery = SessionDiscovery(server, dispatcher)
    discovery.run()


def main():
    logging.basicConfig(
        level=logging.NOTSET,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )
    logger.setLevel(logging.DEBUG)

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
