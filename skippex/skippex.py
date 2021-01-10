from abc import ABC, abstractmethod
import argparse
import configparser
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from functools import partial
from io import TextIOBase
import logging
from pathlib import Path
import threading
from time import sleep
from typing import Any, Callable, Dict, Iterator, List, MutableMapping, NamedTuple, Optional, Set, Tuple, cast
from typing_extensions import Literal, TypedDict
from urllib.parse import urlencode
from uuid import UUID, uuid4
import sys
import webbrowser

from plexapi.base import Playable
from plexapi.client import PlexClient
from plexapi.myplex import MyPlexAccount
from plexapi.server import PlexServer
from plexapi.video import Episode
import pychromecast
from pychromecast.controllers.plex import PlexController
import requests
from wrapt import synchronized
import zeroconf

from .notifications import NotificationContainer, NotificationListener

logger = logging.getLogger('skippex')

_APP_NAME = 'Skippex'
_DATABASE_PATH = Path('.skippex.ini')

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
        return self._store.setdefault('app_id', str(uuid4()))

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

    def _make_request(self, method: str, endpoint: str, **kwargs: Dict[str, Any]) -> requests.Response:
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

    def generate_pin(self) -> Tuple[int, str]:
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

    def wait_for_token(self, pin_id: int, pin_code: str, check_interval_sec: int = 1) -> str:
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


@dataclass(frozen=True, eq=False)
class Session:
    key: str
    state: Literal['buffering', 'playing', 'paused', 'stopped']
    playable: Playable
    player: PlexClient

    @classmethod
    def from_playable(cls, playable: Playable) -> 'Session':
        player = playable.players[0]
        return cls(
            key=str(playable.sessionKey),
            state=player.state,
            playable=playable,
            player=player,
        )

    def __hash__(self):
        return hash(self.key)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, self.__class__) and self.key == other.key


@dataclass(frozen=True, eq=False)
class EpisodeSession(Session):
    playable: Episode
    view_offset_ms: int

    @classmethod
    def from_playable(cls, episode: Episode) -> 'EpisodeSession':
        assert not episode.isFullObject()  # Probably dangerous wrt viewOffset otherwise.
        player = episode.players[0]

        return cls(
            key=str(episode.sessionKey),
            state=player.state,
            playable=episode,
            player=player,
            view_offset_ms=int(episode.viewOffset),
        )


class SessionFactory:
    @classmethod
    def make(cls, playable: Playable) -> Session:
        if isinstance(playable, Episode):
            return EpisodeSession.from_playable(playable)
        return Session.from_playable(playable)


class SessionListener(ABC):
    def accept_session(self, session: Session) -> bool:
        """See the docstrings for the callback methods."""
        return True

    @abstractmethod
    def on_session_activity(self, session: Session):
        """Called iff accept_session(session) returns True."""
        pass

    @abstractmethod
    def on_session_removal(self, session: Session):
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
        # XXX: When tested against an iPhone client (iOS 14.3, Plex 7.11, Plex
        # Server 1.21.1.3830), this takes a very long time to return (over 15
        # seconds), even though the client successfully seeks in less than a
        # second. Should probably run this in a new thread.
        self._client.seekTo(offset_ms)


class SeekableChromecastAdapter(Seekable):
    def __init__(self, plex_ctrl: PlexController):
        self._plex_ctrl = plex_ctrl

    def seek(self, offset_ms: int):
        self._plex_ctrl.seek(offset_ms / 1000)


class SeekableNotFoundError(Exception):
    pass


class SeekableProvider(ABC):
    @abstractmethod
    def provide_seekable(self, session: Session) -> Seekable:
        """Raises SeekableNotFoundError if no Seekable could be found."""
        pass


class SeekableProviderChain(SeekableProvider):
    def __init__(self, providers: List[SeekableProvider]):
        self._providers = providers

    def provide_seekable(self, session: Session) -> Seekable:
        for provider in self._providers[:-1]:
            try:
                return provider.provide_seekable(session)
            except SeekableNotFoundError:
                pass
        return self._providers[-1].provide_seekable(session)


class PlexSeekableProvider(SeekableProvider):
    def __init__(self, server: PlexServer):
        self._server = server

    def provide_seekable(self, session: Session) -> Seekable:
        sess_machine_id = session.player.machineIdentifier
        # NOTE: Have to "advertize as player" in order to be considered a client by Plex.
        client: PlexClient
        for client in self._server.clients():
            if client.machineIdentifier == sess_machine_id:
                return SeekablePlexClient(client)
        raise SeekableNotFoundError


class ChromecastNotFoundError(Exception):
    pass


class ChromecastMonitor:
    # The callbacks are called from a thread different from the main thread.

    def __init__(self, listener: pychromecast.CastListener, zconf: zeroconf.Zeroconf):
        self._listener = listener
        self._zconf = zconf
        self._chromecasts: Dict[UUID, pychromecast.Chromecast] = {}

    @synchronized
    def get_chromecast_by_ip(self, ip: str) -> pychromecast.Chromecast:
        for cc in self._chromecasts.values():
            if cc.socket_client.host == ip:
                return cc
        logging.debug(f'Discovered Chromecasts: {self._chromecasts}')
        raise ChromecastNotFoundError(f'could not find Chromecast with address {ip}')

    @synchronized
    def add_callback(self, uuid: UUID, name: str):
        service = self._listener.services[uuid]
        chromecast = pychromecast.get_chromecast_from_service(service, self._zconf)
        chromecast.wait()
        self._chromecasts[uuid] = chromecast
        logger.debug(f'Discovered new Chromecast: {chromecast}')

    @synchronized
    def update_callback(self, uuid: UUID, name: str):
        pass

    @synchronized
    def remove_callback(self, uuid: UUID, name: str, service):
        chromecast = self._chromecasts.pop(uuid)
        logger.debug(f'Removed discovered Chromecast: {chromecast}')


class ChromecastSeekableProvider(SeekableProvider):
    def __init__(self, monitor: ChromecastMonitor):
        self._monitor = monitor

    def provide_seekable(self, session: Session) -> Seekable:
        try:
            chromecast = self._monitor.get_chromecast_by_ip(session.player.address)
        except ChromecastNotFoundError as e:
            raise SeekableNotFoundError from e

        plex_ctrl = PlexController()
        chromecast.register_handler(plex_ctrl)
        return SeekableChromecastAdapter(plex_ctrl)


class SessionExtrapolator(ABC):
    @abstractmethod
    def trigger_extrapolation(self, session: Session, listener_accepted: bool) -> bool:
        pass

    @abstractmethod
    def extrapolate(self, session: Session) -> Tuple[Session, int]:
        """
        Returns the extrapolated session and the extrapolation delay in ms.
        Called iff trigger_extrapolation(session) returns True.
        """
        pass


class AutoSkipper(SessionListener, SessionExtrapolator):
    def __init__(self, seekable_provider: SeekableProvider):
        self._skipped: Set[Session] = set()
        self._sp = seekable_provider

    def trigger_extrapolation(self, session: Session, listener_accepted: bool) -> bool:
        # Note it's only useful to do this when the state is 'playing':
        #  - When it's 'paused', we'll receive another notification either as
        #    soon as the state changes, or every 10 second while it's paused.
        #  - When it's 'buffering', we'll also receive another notification as
        #    soon as the state changes. And I assume we'd also get notified
        #    every 10 second otherwise.
        #  - When it's 'stopped', we've already sent a signal to the dispatcher.

        if not listener_accepted:
            return False

        # The listener accepted the session, and it may have skipped the intro.
        # In that case, we don't wanna extrapolate the session.
        return session not in self._skipped

    def extrapolate(self, session: Session) -> Tuple[Session, int]:
        session = cast(EpisodeSession, session)  # Safe thanks to trigger_extrapolation().
        delay_ms = 1000
        new_view_offset_ms = session.view_offset_ms + delay_ms
        return replace(session, view_offset_ms=new_view_offset_ms), delay_ms

    def accept_session(self, session: Session) -> bool:
        # TODO: Should we skip if the user resumes an episode (even with a new
        # session) during the intro?

        if not isinstance(session, EpisodeSession):
            # Only TV shows have intro markers, other media don't interest us.
            logger.debug('Ignored; not an episode')
            return False

        if session in self._skipped:
            logger.debug('Ignored; already skipped during this session')
            return False

        if session.state != 'playing':
            logger.debug(f'Ignored; state is "{session.state}" instead of "playing"')
            return False

        if not session.playable.hasIntroMarker:
            logger.debug(f'Ignored; has no intro marker')
            return False

        return True

    def on_session_activity(self, session: Session):
        session = cast(EpisodeSession, session)  # Safe thanks to accept_session().
        logger.debug(f'session_activity: {session}')

        intro_marker = next(m for m in session.playable.markers if m.type == 'intro')
        view_offset_ms = session.view_offset_ms

        logger.debug(f'session.key={session.key}')
        logger.debug(f'session.view_offset_ms={session.view_offset_ms}')
        logger.debug(f'intro_marker={intro_marker}')

        if intro_marker.start <= view_offset_ms < intro_marker.end:
            seekable = self._sp.provide_seekable(session)
            seekable.seek(intro_marker.end)
            self._skipped.add(session)
            logger.debug(f'Skipped; seeked from {view_offset_ms} to {intro_marker.end}')
        else:
            logger.debug('Did not skip; not viewing intro')

        logger.debug('-----')

    def on_session_removal(self, session: Session):
        self._skipped.discard(session)


class SessionDispatcher:
    def __init__(self, listener: SessionListener, removal_timeout_sec: int = 20):
        # Plex's session WebSocket announces every 10 second, so we
        # conservatively set the default removal_timeout_sec to 20 seconds.
        self._listener = listener
        self._removal_timeout_sec = removal_timeout_sec
        # Sessions to track and potentially remove after a period of
        # removal_timeout_sec with no dispatching attempt.
        self._last_active: Dict[Session, datetime] = {}

    def dispatch(self, session: Session) -> bool:
        """
        Dispatches the session if listener.accept_session(session) is True.
        Returns the result of that call.
        """
        accepted = False
        if self._listener.accept_session(session):
            accepted = True
            self._listener.on_session_activity(session)

        now = datetime.now()
        self._last_active[session] = now

        # Remove sessions that we haven't seen in the last removal_timeout_sec
        # period, in case dispatch_removal() wasn't called for some reason.
        timeout_ago = now - timedelta(seconds=self._removal_timeout_sec)
        for s, last_active in list(self._last_active.items()):
            if last_active < timeout_ago:
                self._listener.on_session_removal(s)
                del self._last_active[s]

        return accepted

    def dispatch_removal(self, removed_key: SessionKey) -> bool:
        for s in list(self._last_active.keys()):
            if s.key == removed_key:
                self._listener.on_session_removal(s)
                del self._last_active[s]
                return True
        return False


class SessionNotFoundError(Exception):
    pass


class SessionProvider:
    def __init__(self, server: PlexServer):
        self._server = server

    def provide(self, session_key: str) -> Session:
        playable: Playable
        for playable in self._server.sessions():
            if str(playable.sessionKey) == session_key:
                return SessionFactory.make(playable)
        raise SessionNotFoundError


class PlaybackNotification(TypedDict):
    sessionKey: str  # Not an int!
    guid: str  # Can be the empty string.
    ratingKey: str
    url: str  # Can be the empty string.
    key: str
    viewOffset: int  # In milliseconds.
    playQueueItemID: int
    state: Literal['buffering', 'playing', 'paused', 'stopped']


class SessionDiscovery:
    def __init__(
        self,
        server: PlexServer,
        provider: SessionProvider,
        dispatcher: SessionDispatcher,
        extrapolator: SessionExtrapolator,
    ):
        self._server = server
        self._provider = provider
        self._dispatcher = dispatcher
        self._extrapolator = extrapolator

        # To avoid leaks, preserve the following invariant:
        # timer in dict <=> timer alive,
        # where alive = started and not (done executing or cancelled).
        self._timers: Dict[SessionKey, threading.Timer] = {}

    @synchronized
    def alert_callback(self, alert: NotificationContainer):
        if alert['type'] == 'playing':
            # Never seen a case where the alert doesn't contain exactly one
            # notification, but let's loop over the list out of caution.
            for notification in alert['PlaySessionStateNotification']:
                self._handle_notification(notification)

    @synchronized
    def _dispatch_and_schedule_extrapolated(self, session: Session):
        """Dispatches the specified session and potentially extrapolates it."""
        accepted = self._dispatcher.dispatch(session)

        # Preserve the timers invariant: this thread will die if this session
        # doesn't trigger an extrapolation. Note there won't be a dict entry
        # if this function wasn't called as part of a timer, so popping nothing
        # is fine.
        self._timers.pop(session.key, None)

        if not self._extrapolator.trigger_extrapolation(session, accepted):
            logger.debug(f'Will not extrapolate session {session}')
            return

        assert session.key not in self._timers
        new_session, delay_ms = self._extrapolator.extrapolate(session)
        delay_sec = delay_ms / 1000
        new_timer = threading.Timer(
            delay_sec,
            self._dispatch_and_schedule_extrapolated,
            args=(new_session,),
        )
        new_timer.start()
        self._timers[new_session.key] = new_timer

        logger.debug(
            f'Timer (delay={delay_sec:.3f}s) started for extrapolated session '
            f'{new_session} (original: {session})'
        )

    @synchronized
    def _handle_notification(self, notification: PlaybackNotification):
        # Dispatch regular notifications and simulate the rest while extrapoling
        # viewOffset using a timer. When a regular notification comes in, we
        # stop the active timer and handle the notification, then the process
        # repeats.

        # Ensure this is a string because I don't trust the Plex API.
        session_key = str(notification['sessionKey'])
        logger.debug(
            f'Incoming notification for session key {session_key} '
            f'(state = {notification["state"]})'
        )

        # Incoming regular notification, stop the active timer if any. And even
        # though we might recreate one on the spot, let's also remove the dict
        # entry to prevent any leak.
        old_timer = self._timers.pop(session_key, None)
        if old_timer:
            old_timer.cancel()
            logger.debug(f'Cancelled timer for session key {session_key}')
        else:
            logger.debug(f'No existing timer for session key {session_key}')

        if notification['state'] == 'stopped':
            # The HTTP API won't contain the session anymore, so just dispatch
            # the removal and return.
            self._dispatcher.dispatch_removal(session_key)
            return

        try:
            session = self._provider.provide(session_key)
        except SessionNotFoundError:
            if notification['state'] == 'paused':
                # Plex is a little weird and sometimes sends a session
                # on the WebSocket even though the HTTP API doesn't
                # return it. I've seen this happen after opening the
                # Plex web app and noticing that the mini-player at the
                # bottom of the page showed a paused episode. It's
                # probably getting reloaded from memory and sent as a
                # notification, but then they forget to update the API's
                # state. Anyway this is an icky situation and we'll get
                # notified if playback starts anyway, so just return
                # here.
                logger.debug(f"No session found for 'paused' notification")
                return
            raise

        self._dispatch_and_schedule_extrapolated(session)


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
    else:
        # TODO: Ensure we try HTTP only if HTTPS fails.
        server = server_resource.connect()

    # Build the object hierarchy.

    cc_listener = pychromecast.discovery.CastListener()
    zconf = zeroconf.Zeroconf()
    cc_monitor = ChromecastMonitor(cc_listener, zconf)

    cc_listener.add_callback = cc_monitor.add_callback
    cc_listener.update_callback = cc_monitor.update_callback
    cc_listener.remove_callback = cc_monitor.remove_callback
    cc_browser = pychromecast.discovery.start_discovery(cc_listener, zconf)

    seekable_provider = SeekableProviderChain([
        PlexSeekableProvider(server),
        ChromecastSeekableProvider(cc_monitor),
    ])

    session_provider = SessionProvider(server)
    auto_skipper = AutoSkipper(seekable_provider)
    dispatcher = SessionDispatcher(listener=auto_skipper)

    discovery = SessionDiscovery(
        server=server,
        provider=session_provider,
        dispatcher=dispatcher,
        extrapolator=auto_skipper,
    )

    notif_listener = NotificationListener(server, discovery.alert_callback)
    notif_listener.run_forever()


def main():
    logging.basicConfig(
        level=logging.NOTSET,
        format='%(asctime)s - %(threadName)s - %(name)s - %(funcName)s - %(levelname)s - %(message)s',
    )
    logger.setLevel(logging.DEBUG)

    # Disable sub-warning logging for third-party packages.
    for tp_name, tp_logger in logging.root.manager.loggerDict.items():  # type: ignore
        if isinstance(tp_logger, logging.PlaceHolder):
            continue
        if not tp_name.startswith(logger.name):
            tp_logger.setLevel(logging.WARNING)

    _DATABASE_PATH.touch()
    f = lambda: open(_DATABASE_PATH, 'r+')
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
