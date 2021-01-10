import inspect
import json
from typing import Any, Callable, Dict
from urllib.parse import urlparse, urlunparse

from plexapi.server import PlexServer
from typing_extensions import Literal, TypedDict
from websocket import WebSocketApp


class NotificationContainerDict(TypedDict):
    """Type of the underlying dictionary sent in each WebSocket frame.

    See _MessageDict.

    If type is 'playing', the dictionary also has an entry with key
    'PlaySessionStateNotification' of type PlaybackNotification.
    """

    type: str
    size: int

    # if type == 'playing':
    #     PlaySessionStateNotification: PlaybackNotification


class PlaybackNotification(TypedDict):
    """Type of NotificationContainerDict['PlaySessionStateNotification']."""

    sessionKey: str  # Not an int!
    guid: str  # Can be the empty string.
    ratingKey: str
    url: str  # Can be the empty string.
    key: str
    viewOffset: int  # In milliseconds.
    playQueueItemID: int
    state: Literal['buffering', 'playing', 'paused', 'stopped']


class _MessageDict(TypedDict):
    """The format of each WebSocket frame emitted by Plex once parsed."""
    NotificationContainer: NotificationContainerDict


class LoudWebSocketApp(WebSocketApp):
    def _callback(self, callback, *args):
        """The base implementation silences exceptions, unlike this one."""
        if callback:
            if inspect.ismethod(callback):
                callback(*args)
            else:
                callback(self, *args)


class NotificationListener:
    """Cleaner implementation of plexapi.alert.AlertListener.

    By default, it uses an implementation of websocket.WebSocketApp that doesn't
    silence exceptions. It also doesn't needlessly spawn a new thread.
    """

    def __init__(self, server: PlexServer, callback: Callable[[NotificationContainerDict], None]):
        self._server = server
        self._callback = callback

    def _get_ws_url(self) -> str:
        endpoint = '/:/websockets/notifications'
        http_parse = urlparse(self._server.url(endpoint, includeToken=True))
        return urlunparse(http_parse._replace(scheme='ws'))

    def run_forever(self):
        """Listens on the WebSocket and blocks indefinitely."""
        ws_url = self._get_ws_url()
        ws_app = LoudWebSocketApp(ws_url, on_message=self._on_message, on_error=self._on_error)
        ws_app.run_forever()

    def _on_message(self, message: str):
        msg_dict: _MessageDict = json.loads(message)
        self._callback(msg_dict['NotificationContainer'])

    def _on_error(self, e: Exception):
        raise e
