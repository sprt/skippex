from abc import ABC, abstractmethod
from dataclasses import replace
import logging
from time import sleep
from typing import Any, Dict, List, NamedTuple, Optional, Set, Tuple, cast
from urllib.parse import urlencode
from uuid import UUID

from plexapi.client import PlexClient
from plexapi.server import PlexServer
import pychromecast
from pychromecast.controllers.plex import PlexController
import requests
from wrapt import synchronized
import zeroconf

from .sessions import (
    EpisodeSession,
    Session,
    SessionExtrapolator,
    SessionListener,
)


logger = logging.getLogger(__name__)


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
