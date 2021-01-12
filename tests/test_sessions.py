from typing import Set
import pytest
from unittest.mock import Mock

from typing_extensions import Literal

from skippex.notifications import PlaybackNotification
from skippex.sessions import Session, SessionDispatcher, SessionListener


def fake_notification(
    *,
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


@pytest.fixture
def mock_session() -> Mock:
    return Mock(spec=Session)


class TestSessionDispatcher:
    def test_dispatch(
        self,
        mock_session: Mock,
        accept_listener: AcceptListener,
        reject_listener: RejectListener,
    ):
        accept_dispatcher = SessionDispatcher(accept_listener)
        assert accept_dispatcher.dispatch(mock_session)
        assert mock_session in accept_listener.sessions

        reject_dispatcher = SessionDispatcher(reject_listener)
        assert not reject_dispatcher.dispatch(mock_session)
        assert mock_session not in reject_listener.sessions

    def test_dispatch_removal(
        self,
        mock_session: Mock,
        accept_listener: AcceptListener,
    ):
        session_key = '10'
        mock_session.key = session_key

        dispatcher = SessionDispatcher(accept_listener)
        dispatcher.dispatch(mock_session)
        dispatcher.dispatch_removal(session_key)

        assert mock_session not in accept_listener.sessions

    def test_dispatch_removal__discards_inactives(
        self,
        mock_session: Mock,
        accept_listener: AcceptListener,
    ):
        dispatcher = SessionDispatcher(accept_listener, removal_timeout_sec=0)
        dispatcher.dispatch(mock_session)
        assert mock_session not in accept_listener.sessions
