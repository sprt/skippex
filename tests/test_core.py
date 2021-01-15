from unittest.mock import Mock

from plexapi.client import PlexClient
from plexapi.video import Episode
import pytest
from typing_extensions import Literal

from skippex.core import AutoSkipper
from skippex.seekables import SeekableProvider
from skippex.sessions import EpisodeSession, IntroMarker


def make_episode_session(
    *,
    key: str = 'dummy',
    state: Literal['buffering', 'playing', 'paused', 'stopped'] = 'buffering',
    playable: Episode,
    player: PlexClient,
    view_offset_ms: int = -1,
) -> EpisodeSession:
    return EpisodeSession(
        key=key,
        state=state,
        playable=playable,
        player=player,
        view_offset_ms=view_offset_ms,
    )


class TestAutoSkipper:
    @pytest.fixture
    def auto_skipper(self) -> AutoSkipper:
        provider = Mock(spec=SeekableProvider)
        return AutoSkipper(seekable_provider=provider)

    def test_trigger_extrapolation__returns_false_if_past_intro(self, auto_skipper: AutoSkipper):
        playable = Mock()
        playable.markers = [IntroMarker(start=0, end=1000)]

        session = make_episode_session(
            playable=playable,
            view_offset_ms=2000,
            player=Mock(spec=PlexClient),
        )

        assert not auto_skipper.trigger_extrapolation(session, True)
