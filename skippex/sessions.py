from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import threading
from typing import Dict, NamedTuple, Optional, Tuple

from plexapi.base import Playable
from plexapi.client import PlexClient
from plexapi.server import PlexServer
from plexapi.video import Episode
from typing_extensions import Literal
from wrapt import synchronized

from .notifications import NotificationContainerDict, PlaybackNotification


logger = logging.getLogger(__name__)

SessionKey = str


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


class IntroMarker(NamedTuple):
    # In milliseconds.
    start: int
    end: int


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

    def intro_marker(self) -> Optional[IntroMarker]:
        if not self.playable.hasIntroMarker:
            return None
        internal = next(m for m in self.playable.markers if m.type == 'intro')
        return IntroMarker(start=internal.start, end=internal.end)


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
        if isinstance(session, EpisodeSession) and session not in self._last_active:
            logger.info(
                f'New session {session.key}: {session.player} is playing {session.playable} '
                f'(intro marker = {session.playable.hasIntroMarker})'
            )

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
            if last_active <= timeout_ago:
                self._dispatch_removal(s)

        return accepted

    def _dispatch_removal(self, session: Session):
        if isinstance(session, EpisodeSession):
            logger.info(f'Session {session.key} ended: {session.player} stopped playing {session.playable}')
        self._listener.on_session_removal(session)
        del self._last_active[session]

    def dispatch_removal(self, removed_key: SessionKey) -> bool:
        for s in list(self._last_active.keys()):
            if s.key == removed_key:
                self._dispatch_removal(s)
                return True
        return False


class SessionNotFoundError(Exception):
    """Raised by SessionProvider when it could not find a session."""
    pass


class SessionProvider:
    def __init__(self, server: PlexServer):
        self._server = server

    def provide(self, session_key: str) -> Session:
        """Raises SessionNotFoundError when the session could not be found."""
        sessions = self._server.sessions()
        playable: Playable
        for playable in sessions:
            if str(playable.sessionKey) == session_key:
                return SessionFactory.make(playable)
        raise SessionNotFoundError(f'could not find session key {session_key} among {sessions}')


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
    def alert_callback(self, alert: NotificationContainerDict):
        if alert['type'] == 'playing':
            # Never seen a case where the alert doesn't contain exactly one
            # notification, but let's loop over the list out of caution.
            for notification in alert['PlaySessionStateNotification']:  # type: ignore
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
        new_timer.daemon = True
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
            elif notification['state'] == 'buffering':
                # Encountered this issue with an iPhone client that would buffer
                # a lot at the beginning of the session. To be investigated
                # further, but it might be that the HTTP API doesn't return a
                # session, until the first 'playing' notification. Not a huge
                # deal anyway as long as we get our 'playing' notification, so
                # let's just warn here.
                logger.warning(f"No session found for 'buffering' notification")
                return
            raise

        self._dispatch_and_schedule_extrapolated(session)
