from abc import ABC, abstractmethod
import logging
from typing import Dict, List
from uuid import UUID

from plexapi.client import PlexClient
from plexapi.server import PlexServer
import pychromecast
from pychromecast.controllers.plex import PlexController
from wrapt.decorators import synchronized
from zeroconf import Zeroconf

from .sessions import Session


logger = logging.getLogger(__name__)


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
            raise SeekableNotFoundError from e

        plex_ctrl = PlexController()
        chromecast.register_handler(plex_ctrl)
        return SeekableChromecastAdapter(plex_ctrl)
