"""Tests for per-turn thumbs feedback persistence in hermes_state.py."""

import sqlite3

import pytest

import hermes_state
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "test_state.db")
    yield session_db
    session_db.close()


def _turn(db, sid, user="hi", assistant="hello"):
    db.append_message(sid, "user", user)
    return db.append_message(sid, "assistant", assistant)


class TestSchemaMigration:
    def test_reconcile_adds_rating_columns_to_legacy_db(self, tmp_path):
        """A DB created before the rating columns existed must gain them
        via the declarative reconciliation path, not crash."""
        db_path = tmp_path / "legacy.db"
        legacy = (
            hermes_state.SCHEMA_SQL
            .replace("    rating INTEGER,\n", "")
            .replace("    rating_at REAL\n", "")
            .replace(
                "codex_message_items TEXT,\n", "codex_message_items TEXT\n"
            )
            .replace(
                "CREATE INDEX IF NOT EXISTS idx_messages_rating "
                "ON messages(session_id, rating);\n",
                "",
            )
        )
        conn = sqlite3.connect(db_path)
        conn.executescript(legacy)
        conn.commit()
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(messages)"
        ).fetchall()]
        assert "rating" not in cols
        conn.close()

        sdb = SessionDB(db_path=db_path)
        try:
            cols = [r[1] for r in sdb._conn.execute(
                "PRAGMA table_info(messages)"
            ).fetchall()]
            assert "rating" in cols and "rating_at" in cols
            sdb.create_session(session_id="s1", source="cli")
            mid = _turn(sdb, "s1")
            assert sdb.set_message_rating(mid, -1) is True
            assert sdb.get_rating_stats()["down"] == 1
        finally:
            sdb.close()


class TestSetMessageRating:
    def test_set_and_clear(self, db):
        db.create_session(session_id="s1", source="cli")
        mid = _turn(db, "s1")
        assert db.set_message_rating(mid, 1) is True
        assert db.get_rating_stats()["up"] == 1
        assert db.set_message_rating(mid, None) is True
        assert db.get_rating_stats()["total_rated"] == 0
        assert db.set_message_rating(mid, 0) is True  # 0 also clears

    def test_invalid_rating_raises(self, db):
        db.create_session(session_id="s1", source="cli")
        mid = _turn(db, "s1")
        with pytest.raises(ValueError):
            db.set_message_rating(mid, 2)

    def test_unknown_message_id_returns_false(self, db):
        assert db.set_message_rating(999999, 1) is False


class TestLastAssistantMessageId:
    def test_returns_latest_assistant_skipping_tool_rows(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", "user", "q1")
        a1 = db.append_message("s1", "assistant", "a1")
        db.append_message("s1", "tool", "tool output", tool_name="x")
        a2 = db.append_message("s1", "assistant", "a2")
        assert a2 > a1
        assert db.get_last_assistant_message_id("s1") == a2

    def test_none_when_no_assistant(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", "user", "only user")
        assert db.get_last_assistant_message_id("s1") is None


class TestRatingStats:
    def test_score_math_and_since_filter(self, db):
        db.create_session(session_id="s1", source="cli")
        m1 = _turn(db, "s1")
        m2 = _turn(db, "s1")
        m3 = _turn(db, "s1")
        db.set_message_rating(m1, 1)
        db.set_message_rating(m2, 1)
        db.set_message_rating(m3, -1)
        stats = db.get_rating_stats()
        assert stats == {
            "up": 2, "down": 1, "total_rated": 3,
            "score": (2 - 1) / 3,
        }
        # since_ts in the far future excludes everything
        future = db.get_rating_stats(since_ts=2_000_000_000)
        assert future["total_rated"] == 0

    def test_empty_is_zero_score(self, db):
        db.create_session(session_id="s1", source="cli")
        assert db.get_rating_stats()["score"] == 0.0


class TestRatedTurns:
    def test_pairs_user_with_assistant(self, db):
        db.create_session(session_id="s1", source="cli")
        db.append_message("s1", "user", "what is 2+2?")
        a = db.append_message("s1", "assistant", "5")
        db.set_message_rating(a, -1)
        rows = db.get_rated_turns()
        assert len(rows) == 1
        assert rows[0]["user_content"] == "what is 2+2?"
        assert rows[0]["assistant_content"] == "5"
        assert rows[0]["rating"] == -1

    def test_only_returns_rated(self, db):
        db.create_session(session_id="s1", source="cli")
        _turn(db, "s1")  # unrated
        m = _turn(db, "s1")
        db.set_message_rating(m, 1)
        rows = db.get_rated_turns()
        assert len(rows) == 1
        assert rows[0]["message_id"] == m
