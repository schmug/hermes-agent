"""Tests for the autoresearch-style feedback optimizer loop."""

from unittest.mock import patch

import pytest

import agent.feedback_optimizer as fo
from hermes_state import SessionDB


@pytest.fixture()
def db(tmp_path):
    session_db = SessionDB(db_path=tmp_path / "fb_state.db")
    session_db.create_session(session_id="s1", source="cli")
    yield session_db
    session_db.close()


def _down_turn(db, i):
    db.append_message("s1", "user", f"question {i}")
    m = db.append_message("s1", "assistant", f"weak answer {i}")
    db.set_message_rating(m, -1)


class TestGating:
    def test_disabled_by_default(self):
        assert fo.is_enabled() is False

    def test_disabled_run_is_noop(self, db):
        msg = fo.run_feedback_optimization(session_db=db, force=False)
        assert "disabled" in msg

    def test_below_threshold_does_not_propose(self, db):
        _down_turn(db, 0)
        with patch.object(fo, "is_enabled", return_value=True):
            msg = fo.run_feedback_optimization(session_db=db, force=False)
        assert "gathering more signal" in msg

    def test_healthy_signal_skips(self, db):
        # Many thumbs up, no down -> nothing to do even when enabled.
        for i in range(20):
            db.append_message("s1", "user", f"q{i}")
            m = db.append_message("s1", "assistant", f"a{i}")
            db.set_message_rating(m, 1)
        with patch.object(fo, "is_enabled", return_value=True):
            msg = fo.run_feedback_optimization(session_db=db, force=False)
        assert "healthy" in msg


class TestProposeAndTrial:
    def test_propose_opens_trial_then_keep(self, db):
        for i in range(20):
            _down_turn(db, i)
        with patch.object(
            fo, "_run_llm_pass",
            return_value={"final": "edited SOUL", "summary": "tightened guidance", "error": None},
        ), patch.object(fo, "_snapshot", return_value="snap1"):
            msg = fo.run_feedback_optimization(session_db=db, force=True)
        assert "under trial" in msg
        state = fo.load_state()
        assert state["pending_trial"]["snapshot_id"] == "snap1"

        # New ratings come in better -> keep on next evaluation.
        for i in range(20):
            db.append_message("s1", "user", f"good q{i}")
            mm = db.append_message("s1", "assistant", f"good a{i}")
            db.set_message_rating(mm, 1)
        with patch.object(fo, "_restore") as restore:
            msg2 = fo.run_feedback_optimization(session_db=db, force=True)
        assert "kept" in msg2
        restore.assert_not_called()
        assert fo.load_state()["pending_trial"] is None

    def test_regression_triggers_restore(self, db):
        # Mixed baseline (some up, more down) so the trial window can
        # actually regress below it.
        for i in range(8):
            db.append_message("s1", "user", f"ok q{i}")
            mm = db.append_message("s1", "assistant", f"ok a{i}")
            db.set_message_rating(mm, 1)
        for i in range(12):
            _down_turn(db, i)
        with patch.object(
            fo, "_run_llm_pass",
            return_value={"final": "x", "summary": "made a change", "error": None},
        ), patch.object(fo, "_snapshot", return_value="snapA"):
            fo.run_feedback_optimization(session_db=db, force=True)
        # Trial window: more thumbs-down -> score regresses -> revert.
        for i in range(20):
            db.append_message("s1", "user", f"still bad {i}")
            mm = db.append_message("s1", "assistant", f"still bad a{i}")
            db.set_message_rating(mm, -1)
        with patch.object(fo, "_restore", return_value=True) as restore:
            msg = fo.run_feedback_optimization(session_db=db, force=True)
        restore.assert_called_once_with("snapA")
        assert "reverted" in msg
        assert fo.load_state()["pending_trial"] is None

    def test_llm_no_change_drops_snapshot_no_trial(self, db):
        for i in range(20):
            _down_turn(db, i)
        with patch.object(
            fo, "_run_llm_pass",
            return_value={"final": "no change", "summary": "no change", "error": None},
        ), patch.object(fo, "_snapshot", return_value="snapN"):
            msg = fo.run_feedback_optimization(session_db=db, force=True)
        assert "no change" in msg
        assert fo.load_state()["pending_trial"] is None

    def test_llm_error_reverts_and_no_trial(self, db):
        for i in range(20):
            _down_turn(db, i)
        with patch.object(
            fo, "_run_llm_pass",
            return_value={"final": "", "summary": "", "error": "boom"},
        ), patch.object(fo, "_snapshot", return_value="snapE"), \
                patch.object(fo, "_restore", return_value=True) as restore:
            msg = fo.run_feedback_optimization(session_db=db, force=True)
        restore.assert_called_once_with("snapE")
        assert "failed" in msg
        assert fo.load_state()["pending_trial"] is None


class TestSnapshotRestore:
    def test_roundtrip(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / "memories").mkdir(parents=True)
        soul = home / "SOUL.md"
        soul.write_text("original soul", encoding="utf-8")
        (home / "memories" / "MEMORY.md").write_text("orig mem", encoding="utf-8")
        monkeypatch.setattr(fo, "get_hermes_home", lambda: home)

        snap = fo._snapshot()
        assert snap is not None
        soul.write_text("CORRUPTED", encoding="utf-8")
        (home / "memories" / "MEMORY.md").write_text("BAD", encoding="utf-8")

        assert fo._restore(snap) is True
        assert soul.read_text(encoding="utf-8") == "original soul"
        assert (home / "memories" / "MEMORY.md").read_text(
            encoding="utf-8"
        ) == "orig mem"


class TestShouldRunNow:
    def test_disabled_returns_false(self):
        assert fo.should_run_now() is False

    def test_pending_trial_forces_run(self, monkeypatch):
        monkeypatch.setattr(fo, "is_enabled", lambda: True)
        with patch.object(
            fo, "load_state",
            return_value={"pending_trial": {"snapshot_id": "x"}},
        ):
            assert fo.should_run_now() is True
