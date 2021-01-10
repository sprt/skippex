from dataclasses import replace
import logging
from typing import Set, Tuple, cast

from .seekables import SeekableProvider
from .sessions import (
    EpisodeSession,
    Session,
    SessionExtrapolator,
    SessionListener,
)


logger = logging.getLogger(__name__)


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
