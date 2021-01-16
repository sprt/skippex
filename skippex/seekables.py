from abc import ABC, abstractmethod
import logging
import threading
from typing import Dict, List, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from uuid import UUID

from plexapi.client import DEFAULT_MTYPE, PlexClient
from plexapi.server import PlexServer
import pychromecast
from pychromecast.controllers.plex import PlexController
import requests
from wrapt.decorators import synchronized
from zeroconf import Zeroconf

from .sessions import Session


logger = logging.getLogger(__name__)


def _removesuffix(s: str, suffix: str) -> str:
    if s.endswith(suffix):
        return s[:-len(suffix)]
    return s


class Seekable(ABC):
    @abstractmethod
    def seek(self, offset_ms: int):
        pass


class SeekablePlexClient(Seekable):
    _TIMEOUT_SUFFIX = '-timeout'

    def __init__(self, client: PlexClient, timeout_sec: float = 5):
        self._client = client
        self._timeout_sec = timeout_sec

        # Save the original PlexClient.query() for future monkey patching in
        # seek(). Save it here and not in seek() to avoid a potential stack
        # overflow (with nested method calls) at run-time if we patch a lot.
        self._original_query = self._client.query

        # HACK: Monkey patch PlexClient.query() so that it takes the specified
        # timeout into account for the seekTo command.
        self._client.query = self._patched_query

    # Same signature as PlexClient.query().
    def _patched_query(self, path, method=None, headers=None, timeout=None, **kwargs):
        """Patched implementation of self._client.query()."""
        p = urlparse(path)
        new_qsl: List[Tuple[str, str]] = []

        for k, v in parse_qsl(p.query):
            if k == 'type':
                new_v = _removesuffix(v, self._TIMEOUT_SUFFIX)
                if new_v != v:
                    # The suffix was present, override the timeout.
                    timeout = self._timeout_sec
                new_qsl.append((k, new_v))
            else:
                new_qsl.append((k, v))

        new_query = urlencode(new_qsl, doseq=True)
        new_path = urlunparse(p._replace(query=new_query))

        return self._original_query(
            path=new_path,
            method=method,
            headers=headers,
            timeout=timeout,
            **kwargs
        )

    def seek(self, offset_ms: int):
        """Sends the seeking command in a non-blocking fashion.

        When tested against an iPhone client (iOS 14.3, Plex for iOS 7.11, Plex
        Media Server 1.21.1.3830), the seeking command takes a long time (over
        15 seconds) to issue a response, even though the client successfully
        seeks in less than a second. Therefore, we send the seeking command in
        a new thread and we only log what happens.
        """
        def _seek():
            def log_timeout_warning():
                # About "Advertize as player": If the user disables that setting
                # while Skippex is running, seeking will timeout (and not
                # happen), even though PlexSeekableProvider found the client.
                logger.warning(
                    f'Seeking command timed out for {self._client}, but '
                    f'seeking might still have happened. If not, please ensure '
                    f'that the "Advertize as player" setting is enabled for '
                    f'your client.'
                )

            try:
                # HACK: We add a suffix to mtype to signal to the patched method
                # to use the timeout set on this instance. This is the only way
                # we have to "pass a message" to PlexClient.query() from this
                # call.
                self._client.seekTo(offset_ms, mtype=DEFAULT_MTYPE+self._TIMEOUT_SUFFIX)
            except requests.Timeout:
                log_timeout_warning()
            except requests.ConnectionError as e:
                # See https://github.com/psf/requests/issues/5430.
                if 'timed out' in str(e):
                    log_timeout_warning()
                raise
            except Exception:
                logger.exception(f'Seeking failed for {self._client}')
            else:
                logger.debug(f'Seeking succeeded for {self._client}')

        thread = threading.Thread(target=_seek, daemon=True)
        thread.start()
        logger.debug(f'Sent seeking command to {self._client}')


class SeekableChromecastAdapter(Seekable):
    def __init__(self, plex_ctrl: PlexController):
        self._plex_ctrl = plex_ctrl

    def seek(self, offset_ms: int):
        self._plex_ctrl.seek(offset_ms / 1000)


class SeekableNotFoundError(Exception):
    def has_plex_player_not_found(self) -> bool:
        return isinstance(self, PlexPlayerNotFoundError)


class PlexPlayerNotFoundError(SeekableNotFoundError):
    pass


class SeekableNotFoundErrorChain(SeekableNotFoundError):
    def __init__(self, exceptions: List[SeekableNotFoundError], *args: object):
        super().__init__(exceptions, *args)
        self.exceptions = exceptions

    def has_plex_player_not_found(self) -> bool:
        for e in self.exceptions:
            if isinstance(e, PlexPlayerNotFoundError):
                return True
        return False


class SeekableProvider(ABC):
    @abstractmethod
    def provide_seekable(self, session: Session) -> Seekable:
        """Raises SeekableNotFoundError if no Seekable could be found."""
        pass


class SeekableProviderChain(SeekableProvider):
    def __init__(self, providers: List[SeekableProvider]):
        self._providers = providers

    def provide_seekable(self, session: Session) -> Seekable:
        exceptions = []
        for provider in self._providers:
            try:
                return provider.provide_seekable(session)
            except SeekableNotFoundError as e:
                exceptions.append(e)
        else:
            raise SeekableNotFoundErrorChain(exceptions)


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
        raise PlexPlayerNotFoundError(f'could not find Plex player with machine ID {sess_machine_id}')


class ChromecastNotFoundError(Exception):
    pass


class ChromecastMonitor:
    # The callbacks are called from a thread different from the main thread.

    def __init__(self, listener: pychromecast.CastListener, zconf: Zeroconf):
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
        # No-op.
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
            raise SeekableNotFoundError(
                f'could not find Chromecast with address {session.player.address}'
            ) from e

        plex_ctrl = PlexController()
        chromecast.register_handler(plex_ctrl)
        return SeekableChromecastAdapter(plex_ctrl)
