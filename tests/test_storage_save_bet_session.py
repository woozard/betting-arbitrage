"""save_bet must use its own short-lived session (thread-safe for async finalize)."""

import logging
from contextlib import contextmanager

import utils.storage as storage_mod
from utils.storage import Storage


class _FakeSession:
    def __init__(self):
        self.added = []
        self.committed = False

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.committed = True


def _bet():
    return {
        "sport": "MLB", "league": "MLB", "game_id": "g1",
        "game_datetime": "2026-07-11 20:00:00",
        "team_1": "Athletics", "team_2": "Chicago White Sox",
        "bookmaker": "4casters", "bet_type": "moneyline",
        "team_no": 2, "team_name": "Chicago White Sox",
        "odds": "-118", "stake": 118.0,
    }


def test_save_bet_uses_independent_session(monkeypatch):
    fake = _FakeSession()

    @contextmanager
    def _fake_scope():
        yield fake

    monkeypatch.setattr(storage_mod, "db1_session_scope", _fake_scope)

    st = Storage(logging.getLogger("test-storage"))
    # Guard: if save_bet touched the shared session, this would blow up.
    st.db = None

    ok = st.save_bet(_bet())

    assert ok is True
    assert fake.committed is True
    assert len(fake.added) == 1
