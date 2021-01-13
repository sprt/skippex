from typing import Set
from unittest.mock import Mock

from plexapi.base import Playable
from plexapi.client import PlexClient
from plexapi.server import PlexServer
import pytest
from typing_extensions import Literal

from skippex.notifications import PlaybackNotification
from skippex.sessions import (
    Session,
    SessionDiscovery,
    SessionDispatcher,
    SessionExtrapolator,
    SessionListener,
    SessionNotFoundError,
    SessionProvider,
)


def make_fake_notification(
    sessionKey: str = 'dummy',
    guid: str = 'dummy',
    ratingKey: str = 'dummy',
    url: str = 'dummy',
    key: str = 'dummy',
    viewOffset: int = -1,
    playQueueItemID: int = -1,
    state: Literal['buffering', 'playing', 'paused', 'stopped'] = 'buffering',
) -> PlaybackNotification:
    return PlaybackNotification(
        sessionKey=sessionKey,
        guid=guid,
        ratingKey=ratingKey,
        url=url,
        key=key,
        viewOffset=viewOffset,
        playQueueItemID=playQueueItemID,
        state=state,
    )


def make_fake_session(
    key: str = 'dummy',
    state: Literal['buffering', 'playing', 'paused', 'stopped'] = 'buffering',
    playable: Playable = Mock(spec=Playable),
    player: PlexClient = Mock(spec=PlexClient),
) -> Session:
    return Session(
        key=key,
        state=state,
        playable=playable,
        player=player,
    )


@pytest.fixture
def fake_session() -> Session:
    return make_fake_session()


class FakeListener(SessionListener):
    """Stores active sessions in a set."""

    def __init__(self):
        self.sessions: Set[Session] = set()

    def on_session_activity(self, session: Session):
        self.sessions.add(session)

    def on_session_removal(self, session: Session):
        self.sessions.remove(session)


class AcceptListener(FakeListener):
    def accept_session(self, session: Session) -> bool:
        return True


class RejectListener(FakeListener):
    def accept_session(self, session: Session) -> bool:
        return False


@pytest.fixture
def accept_listener() -> AcceptListener:
    return AcceptListener()


@pytest.fixture
def reject_listener() -> RejectListener:
    return RejectListener()


class TestSession:
    s1 = make_fake_session(key='1')
    s1bis = make_fake_session(key='1')
    s2 = make_fake_session(key='2')
    s1_playing = make_fake_session(key='1', state='playing')
    s1_paused = make_fake_session(key='1', state='paused')

    is_same_cases = [
        (s1, s2, False),
        (s1, s1, True),
        (s2, s2, True),
        (s1, s1bis, True),
        (s1_playing, s1_paused, True),
    ]

    @pytest.mark.parametrize('a, b, is_same', is_same_cases)
    def test_hash(self, a: Session, b: Session, is_same: bool):
        assert (hash(a) == hash(b)) is is_same

    @pytest.mark.parametrize('a, b, is_same', is_same_cases)
    def test_eq(self, a: Session, b: Session, is_same: bool):
        assert (a == b) is is_same


class TestSessionDispatcher:
    def test_dispatch(
        self,
        fake_session: Session,
        accept_listener: AcceptListener,
        reject_listener: RejectListener,
    ):
        accept_dispatcher = SessionDispatcher(accept_listener)
        assert accept_dispatcher.dispatch(fake_session)
        assert fake_session in accept_listener.sessions

        reject_dispatcher = SessionDispatcher(reject_listener)
        assert not reject_dispatcher.dispatch(fake_session)
        assert fake_session not in reject_listener.sessions

    def test_dispatch_removal(
        self,
        accept_listener: AcceptListener,
    ):
        session_key = '10'
        session = make_fake_session(key=session_key)

        dispatcher = SessionDispatcher(accept_listener)
        dispatcher.dispatch(session)
        dispatcher.dispatch_removal(session_key)

        assert fake_session not in accept_listener.sessions

    def test_dispatch_removal__discards_inactives(
        self,
        fake_session: Session,
        accept_listener: AcceptListener,
    ):
        dispatcher = SessionDispatcher(accept_listener, removal_timeout_sec=0)
        dispatcher.dispatch(fake_session)
        assert fake_session not in accept_listener.sessions


class TestSessionDiscovery:
    buffering_notif = make_fake_notification(state='buffering')
    paused_notif = make_fake_notification(state='paused')

    @pytest.mark.parametrize('notif', [buffering_notif, paused_notif])
    def test_handle_notification__missing_notification_does_not_raise(self, notif: PlaybackNotification):
        provider = Mock(spec=SessionProvider)
        provider.provide.side_effect = Mock(side_effect=SessionNotFoundError)

        server = Mock(spec=PlexServer)
        dispatcher = Mock(spec=SessionDispatcher)
        extrapolator = Mock(spec=SessionExtrapolator)

        discovery = SessionDiscovery(
            server=server,
            provider=provider,
            dispatcher=dispatcher,
            extrapolator=extrapolator,
        )
        discovery._handle_notification(notif)  # Shouldn't raise.
