"""Feedback optimizer — an autoresearch-style self-improvement loop.

Inspired by karpathy/autoresearch: an agent repeatedly edits a target,
runs under a budget, evaluates a metric, and keeps a change only if the
metric does not regress — otherwise it reverts and tries again. Hermes
calls external LLM providers and trains no weights, so the editable
"target" here is the persistent persona/memory/skills (SOUL.md, the
memories/ directory, and agent-created skills) and the "metric" is the
aggregate human thumbs up/down signal collected per turn.

The loop is deliberately slow and conservative. Human feedback is sparse,
so a single pass either:

  1. evaluates a previously-applied change once enough new ratings have
     accumulated (keep if no significant regression, else restore the
     snapshot), or
  2. proposes one bounded round of edits via a forked AIAgent when the
     signal is negative and there is no change under trial.

Strict invariants:
  - Disabled by default (``feedback_optimizer.enabled`` must be set True).
  - Never deletes user content — operates strictly snapshot-and-restore.
  - One change under trial at a time; mandatory restore on regression.
  - Uses a forked auxiliary agent; never touches the live session.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

DEFAULT_ENABLED = False
DEFAULT_INTERVAL_HOURS = 168          # weekly
DEFAULT_MIN_RATED_TURNS = 15          # need this many before proposing
DEFAULT_TRIAL_MIN_NEW_RATINGS = 10    # new ratings before judging a trial
DEFAULT_REGRESSION_TOLERANCE = 0.05   # allowed score drop before revert
DEFAULT_HEALTHY_SCORE = 0.5           # skip proposing when signal is good
_BACKUP_KEEP = 5


# ---------------------------------------------------------------------------
# Config + state
# ---------------------------------------------------------------------------

def _load_config() -> Dict[str, Any]:
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except Exception as e:
        logger.debug("feedback_optimizer: config load failed: %s", e)
        return {}
    if not isinstance(cfg, dict):
        return {}
    sub = cfg.get("feedback_optimizer") or {}
    return sub if isinstance(sub, dict) else {}


def _cfg_int(key: str, default: int) -> int:
    try:
        return int(_load_config().get(key, default))
    except (TypeError, ValueError):
        return default


def _cfg_float(key: str, default: float) -> float:
    try:
        return float(_load_config().get(key, default))
    except (TypeError, ValueError):
        return default


def is_enabled() -> bool:
    """Default OFF. The loop mutates persona/memory/skills, so it is
    strictly opt-in via ``feedback_optimizer.enabled: true``."""
    return bool(_load_config().get("enabled", DEFAULT_ENABLED))


def _state_file() -> Path:
    return get_hermes_home() / ".feedback_optimizer_state"


def _default_state() -> Dict[str, Any]:
    return {
        "last_run_at": None,
        "run_count": 0,
        "last_summary": None,
        "pending_trial": None,   # dict: see _propose_changes
        "history": [],           # list of {at, action, detail}
    }


def load_state() -> Dict[str, Any]:
    path = _state_file()
    if not path.exists():
        return _default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            base = _default_state()
            base.update({k: v for k, v in data.items() if k in base})
            return base
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("feedback_optimizer: state read failed: %s", e)
    return _default_state()


def save_state(data: Dict[str, Any]) -> None:
    path = _state_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=".fbopt_", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as e:
        logger.debug("feedback_optimizer: state save failed: %s", e)


def _record_history(state: Dict[str, Any], action: str, detail: str) -> None:
    hist = state.setdefault("history", [])
    hist.append({
        "at": datetime.now(timezone.utc).isoformat(),
        "action": action,
        "detail": detail,
    })
    # Keep the audit trail bounded.
    if len(hist) > 50:
        del hist[:-50]


def should_run_now() -> bool:
    """Static gate: enabled, and either a trial is pending or the interval
    has elapsed. Threshold/idle checks happen at the call site where the
    rating counts are known."""
    if not is_enabled():
        return False
    state = load_state()
    if state.get("pending_trial"):
        return True
    last = state.get("last_run_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    elapsed_h = (
        datetime.now(timezone.utc) - last_dt
    ).total_seconds() / 3600.0
    return elapsed_h >= _cfg_int("interval_hours", DEFAULT_INTERVAL_HOURS)


# ---------------------------------------------------------------------------
# Snapshot / restore (snapshot-and-restore is the only mutation safety net)
# ---------------------------------------------------------------------------

def _targets() -> List[Path]:
    home = get_hermes_home()
    return [home / "SOUL.md", home / "memories", home / "skills"]


def _backups_root() -> Path:
    return get_hermes_home() / ".feedback_optimizer_backups"


def _snapshot() -> Optional[str]:
    """Copy the editable targets into a timestamped backup dir. Returns the
    backup id, or None if nothing could be snapshotted."""
    root = _backups_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.debug("feedback_optimizer: cannot create backups dir: %s", e)
        return None
    snap_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = snap_id
    n = 1
    while (root / snap_id).exists():
        snap_id = f"{base}-{n:02d}"
        n += 1
    dest = root / snap_id
    try:
        dest.mkdir(parents=True, exist_ok=False)
        for target in _targets():
            if not target.exists():
                continue
            if target.is_dir():
                shutil.copytree(target, dest / target.name)
            else:
                shutil.copy2(target, dest / target.name)
    except OSError as e:
        logger.debug("feedback_optimizer: snapshot failed: %s", e)
        shutil.rmtree(dest, ignore_errors=True)
        return None
    _prune_backups()
    return snap_id


def _restore(snap_id: str) -> bool:
    """Restore a snapshot, overwriting the live targets. Returns True on
    success."""
    src = _backups_root() / snap_id
    if not src.exists():
        logger.debug("feedback_optimizer: snapshot %s missing", snap_id)
        return False
    home = get_hermes_home()
    try:
        for target in _targets():
            backup = src / target.name
            if not backup.exists():
                continue
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.exists():
                target.unlink()
            if backup.is_dir():
                shutil.copytree(backup, home / target.name)
            else:
                shutil.copy2(backup, home / target.name)
        return True
    except OSError as e:
        logger.debug("feedback_optimizer: restore failed: %s", e)
        return False


def _prune_backups() -> None:
    root = _backups_root()
    try:
        snaps = sorted(
            [p for p in root.iterdir() if p.is_dir()], key=lambda p: p.name
        )
    except OSError:
        return
    for old in snaps[:-_BACKUP_KEEP]:
        shutil.rmtree(old, ignore_errors=True)


# ---------------------------------------------------------------------------
# Proposal pass — a forked AIAgent edits the targets given the evidence
# ---------------------------------------------------------------------------

_PROMPT_TEMPLATE = """\
You are Hermes's self-improvement reviewer. Users give an optional thumbs
up or thumbs down on individual assistant turns. Below are recent turns
that received a THUMBS DOWN. Your job: make small, targeted improvements
to the agent's persistent guidance so future turns of this kind go better.

You may edit ONLY these targets (use the file and skill tools):
  - {soul} (the persistent persona / standing instructions)
  - the memories/ directory (MEMORY.md, USER.md)
  - agent-created skills (via skill_manage)

Hard rules:
  - Make at most ONE skill change and a SMALL, focused edit to SOUL.md
    or memory. Do not rewrite files wholesale.
  - Do not delete user content. Prefer additive, clarifying guidance.
  - If the negative turns share no actionable pattern, change NOTHING and
    say so. A no-op is a valid, good outcome.

Thumbs-down turns ({n} shown):
{evidence}

Positive signal for reference: {up} thumbs up vs {down} thumbs down
recently (score {score:+.2f}).

Begin by identifying the common failure pattern, then apply the minimal
edit that addresses it. End with a one-sentence summary of what you
changed (or "no change").
"""


def _format_evidence(turns: List[Dict[str, Any]], limit: int = 12) -> str:
    lines = []
    for i, t in enumerate(turns[:limit], 1):
        user = (t.get("user_content") or "").strip().replace("\n", " ")
        asst = (t.get("assistant_content") or "").strip().replace("\n", " ")
        if len(user) > 500:
            user = user[:500] + "…"
        if len(asst) > 800:
            asst = asst[:800] + "…"
        lines.append(f"[{i}] User: {user}\n    Hermes: {asst}")
    return "\n\n".join(lines) if lines else "(none)"


def _run_llm_pass(prompt: str) -> Dict[str, Any]:
    """Fork an auxiliary AIAgent to run the proposal prompt. Mirrors
    agent.curator._run_llm_review's resolution + isolation approach. Never
    raises."""
    import contextlib

    meta: Dict[str, Any] = {"final": "", "summary": "", "error": None}
    try:
        from run_agent import AIAgent
    except Exception as e:
        meta["error"] = f"AIAgent import failed: {e}"
        return meta

    api_key = base_url = api_mode = resolved_provider = None
    model_name = ""
    try:
        from hermes_cli.config import load_config
        from hermes_cli.runtime_provider import resolve_runtime_provider
        try:
            from agent.curator import _resolve_review_runtime
            binding = _resolve_review_runtime(load_config())
            provider, model_name = binding.provider, binding.model
            rp = resolve_runtime_provider(
                requested=provider,
                target_model=model_name,
                explicit_api_key=binding.explicit_api_key,
                explicit_base_url=binding.explicit_base_url,
            )
        except Exception:
            rp = resolve_runtime_provider(requested=None, target_model="")
            provider = rp.get("provider")
        api_key = rp.get("api_key")
        base_url = rp.get("base_url")
        api_mode = rp.get("api_mode")
        resolved_provider = rp.get("provider") or provider
    except Exception as e:
        logger.debug("feedback_optimizer: provider resolution failed: %s", e)

    agent = None
    try:
        agent = AIAgent(
            model=model_name,
            provider=resolved_provider,
            api_key=api_key,
            base_url=base_url,
            api_mode=api_mode,
            max_iterations=200,
            quiet_mode=True,
            platform="feedback_optimizer",
            skip_context_files=True,
            skip_memory=True,
        )
        agent._memory_nudge_interval = 0
        agent._skill_nudge_interval = 0
        with open(os.devnull, "w", encoding="utf-8") as devnull, \
                contextlib.redirect_stdout(devnull), \
                contextlib.redirect_stderr(devnull):
            res = agent.run_conversation(user_message=prompt)
        final = ""
        if isinstance(res, dict):
            final = str(res.get("final_response") or "").strip()
        meta["final"] = final
        meta["summary"] = (
            (final[:240] + "…") if len(final) > 240 else (final or "no change")
        )
    except Exception as e:
        meta["error"] = f"error: {e}"
    finally:
        if agent is not None:
            try:
                agent.close()
            except Exception:
                pass
    return meta


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def _evaluate_pending_trial(
    state: Dict[str, Any], session_db, force: bool
) -> Optional[str]:
    """Judge a change that was applied on a previous run. Returns a summary
    string if the trial was resolved, else None (not enough new ratings)."""
    trial = state.get("pending_trial")
    if not trial:
        return None
    started = float(trial.get("started_ts") or 0)
    try:
        cur = session_db.get_rating_stats(since_ts=started)
    except Exception as e:
        return f"feedback optimize: could not read ratings ({e})"

    min_new = _cfg_int(
        "trial_min_new_ratings", DEFAULT_TRIAL_MIN_NEW_RATINGS
    )
    if not force and cur["total_rated"] < min_new:
        return (
            f"feedback optimize: change under trial — "
            f"{cur['total_rated']}/{min_new} new ratings so far, "
            f"holding."
        )

    baseline = float(trial.get("baseline_score") or 0.0)
    tol = _cfg_float("regression_tolerance", DEFAULT_REGRESSION_TOLERANCE)
    new_score = cur["score"]
    state["pending_trial"] = None

    if new_score < baseline - tol:
        restored = _restore(trial.get("snapshot_id") or "")
        verdict = "reverted" if restored else "revert FAILED"
        _record_history(
            state, "discard",
            f"score {new_score:+.2f} < baseline {baseline:+.2f} "
            f"(tol {tol}); {verdict}",
        )
        return (
            f"feedback optimize: change regressed signal "
            f"({baseline:+.2f} → {new_score:+.2f}); {verdict}."
        )

    _record_history(
        state, "keep",
        f"score {new_score:+.2f} >= baseline {baseline:+.2f} - {tol}",
    )
    return (
        f"feedback optimize: change kept "
        f"(signal {baseline:+.2f} → {new_score:+.2f})."
    )


def _propose_changes(
    state: Dict[str, Any], agent, session_db, force: bool
) -> str:
    try:
        stats = session_db.get_rating_stats()
    except Exception as e:
        return f"feedback optimize: could not read ratings ({e})"

    min_turns = _cfg_int("min_rated_turns", DEFAULT_MIN_RATED_TURNS)
    if not force and stats["total_rated"] < min_turns:
        return (
            f"feedback optimize: only {stats['total_rated']}/{min_turns} "
            f"rated turns — gathering more signal before acting."
        )

    healthy = _cfg_float("healthy_score", DEFAULT_HEALTHY_SCORE)
    if not force and (stats["down"] == 0 or stats["score"] >= healthy):
        state["last_run_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return (
            f"feedback optimize: signal healthy "
            f"(score {stats['score']:+.2f}); nothing to do."
        )

    try:
        down_turns = [
            t for t in session_db.get_rated_turns(limit=200)
            if t.get("rating") == -1
        ]
    except Exception as e:
        return f"feedback optimize: could not load rated turns ({e})"
    if not down_turns:
        return "feedback optimize: no thumbs-down turns to learn from."

    snap_id = _snapshot()
    if snap_id is None:
        return (
            "feedback optimize: could not snapshot targets; "
            "aborting (refusing to edit without a safety net)."
        )

    prompt = _PROMPT_TEMPLATE.format(
        soul=str(get_hermes_home() / "SOUL.md"),
        n=min(len(down_turns), 12),
        evidence=_format_evidence(down_turns),
        up=stats["up"],
        down=stats["down"],
        score=stats["score"],
    )
    result = _run_llm_pass(prompt)
    now = datetime.now(timezone.utc)

    if result.get("error"):
        # The pass failed before/while editing — restore to be safe and
        # do not open a trial.
        _restore(snap_id)
        state["last_run_at"] = now.isoformat()
        _record_history(state, "error", result["error"])
        save_state(state)
        return f"feedback optimize: proposal pass failed; reverted ({result['error']})."

    summary = result.get("summary") or "no change"
    no_change = summary.strip().lower() in {"no change", "no change."}

    state["last_run_at"] = now.isoformat()
    state["run_count"] = int(state.get("run_count", 0)) + 1
    state["last_summary"] = summary

    if no_change:
        # Nothing was changed; drop the snapshot, no trial needed.
        _record_history(state, "noop", summary)
        save_state(state)
        return f"feedback optimize: reviewer made no change ({summary})."

    state["pending_trial"] = {
        "snapshot_id": snap_id,
        "baseline_score": stats["score"],
        "baseline_total": stats["total_rated"],
        "started_ts": time.time(),
        "summary": summary,
    }
    _record_history(state, "propose", summary)
    save_state(state)
    return (
        f"feedback optimize: applied a change, now under trial — "
        f"{summary}"
    )


def run_feedback_optimization(
    *, agent=None, session_db=None, force: bool = False
) -> str:
    """Run one optimization step. Either resolves a pending trial or
    proposes a new bounded change. Returns a human-readable summary.

    ``force`` (manual ``/feedback optimize``) bypasses the interval and
    sample-size gates but never bypasses the snapshot safety net.
    """
    if session_db is None:
        try:
            from hermes_state import SessionDB
            session_db = SessionDB()
        except Exception as e:
            return f"feedback optimize: no session database ({e})"

    if not force and not is_enabled():
        return "feedback optimize: disabled (feedback_optimizer.enabled)."

    state = load_state()

    resolved = _evaluate_pending_trial(state, session_db, force)
    if resolved is not None:
        # If the trial just resolved, persist and stop here — proposing a
        # new change in the same pass would muddy the next measurement.
        if state.get("pending_trial") is None:
            state["last_run_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return resolved

    return _propose_changes(state, agent, session_db, force)


def maybe_run_feedback_optimization(
    *,
    idle_for_seconds: Optional[float] = None,
    on_summary: Optional[Callable[[str], None]] = None,
) -> None:
    """Best-effort background trigger, mirroring agent.curator.maybe_run_
    curator. Spawns a daemon thread so it never blocks startup. Never
    raises."""
    try:
        if not should_run_now():
            return
    except Exception:
        return

    def _worker():
        try:
            summary = run_feedback_optimization(force=False)
            if summary and on_summary:
                try:
                    on_summary(summary)
                except Exception:
                    pass
        except Exception as e:
            logger.debug("feedback_optimizer worker failed: %s", e)

    t = threading.Thread(
        target=_worker, name="feedback-optimizer", daemon=True
    )
    t.start()
