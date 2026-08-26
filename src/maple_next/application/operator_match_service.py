"""Official operator match lifecycle extensions.

This module keeps provider/turn fail-closed semantics unchanged while adding
one explicit human recovery behavior required by the desktop operator flow:
a completed match may release the singleton ``active_slot`` so the operator
can return to pre-match team management without creating a new match first.
"""

from __future__ import annotations

from maple_next.application.match_service import MatchApplication
from maple_next.application.service import DomainError
from maple_next.domain.enums import BattleState
from maple_next.domain.models import BattleSession


class OperatorMatchApplication(MatchApplication):
    """Match application used by the official desktop operator entrypoint.

    ``abort_match`` retains the existing behavior for an in-progress/stale
    match: the session is marked ``ABORTED`` and the active slot is released.

    For ``MATCH_ENDED`` and ``MATCH_EXPORTED`` the match is already terminal,
    so rewriting it to ``ABORTED`` would corrupt lifecycle provenance.  The
    explicit operator action therefore releases *only* ``active_slot`` and
    preserves state, outcome, export records, revisions, Selection/Turn data,
    and provider audit history byte-for-byte.
    """

    def abort_match(self, *, human_confirmed: bool) -> BattleSession:
        if not human_confirmed:
            raise DomainError("HUMAN_MATCH_ABORT_CONFIRMATION_REQUIRED")

        with self.repository.transaction():
            session = self._require_active_session()
            if session.state is BattleState.ABORTED:
                raise DomainError("MATCH_ABORT_NOT_ALLOWED_IN_CURRENT_STATE")

            if session.state in {BattleState.MATCH_ENDED, BattleState.MATCH_EXPORTED}:
                # Team-prep release: terminal canonical identity stays intact.
                # In particular, do not bump battle_revision after an export;
                # the persisted/exported final identity must remain unchanged.
                session.active_slot = None
                self.repository.save_session(session)
                return session

            session.state = BattleState.ABORTED
            session.active_slot = None
            session.bump_battle()
            self.repository.save_session(session)
            return session
