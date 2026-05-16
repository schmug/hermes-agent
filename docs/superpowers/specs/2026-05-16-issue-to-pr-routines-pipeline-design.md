# Issue → PR → Merge Pipeline on Claude Code Routines

**Date:** 2026-05-16
**Status:** Approved design — ready for implementation planning
**Owner:** Cory (solo dev, ~4 repos)
**Rollout:** Pilot on **one** repo for a cycle, verify the gate, then expand to
the rest (dmarcheck, donthype-me, benchburner, apartment-stager). Pilot repo TBD
by owner.

## Problem

I want to drive work from my phone: describe intent, have Claude research and
file atomically-sized GitHub issues (spec'd interactively with me), have another
Claude implement each issue into an appropriately-sized PR, and then *not drown
in PRs* — safe ones should merge themselves; the rest should reach me as a
short, ranked, pre-vetted list.

The original guardrail ("never push to main, always human-reviewed PR") was
really about **provenance**: the fear was an *arbitrary external issue* getting
picked up and shipped to prod. An issue I personally researched and spec'd has
trusted lineage; a stranger's drive-by issue does not. The design keys trust on
issue provenance, which dissolves the contradiction and lets the trusted,
low-risk PRs auto-merge.

## Constraints (verified against Claude Code Routines, May 2026)

- **AskUserQuestion cannot run inside a Routine.** Routines are fully
  unattended. Interactive spec'ing must happen in a mobile/web session, *before*
  any Routine. The `/issue` skill is the bridge — it emits a prompt-shaped issue
  body the implementer can execute cold.
- **No first-class auto-merge.** A Routine shells `gh pr merge`; safety must
  come from the provenance/risk gate **plus** GitHub branch protection. Branch
  protection state is **audited first** (implementation step 1) and provisioned
  where missing before any Routine goes live.
- **Per-repo base branch.** The PR target is the repo's integration branch, not
  always `main`. donthype-me uses a `dev→main` flow → its base is `dev`. Base
  branch is per-repo config consumed by both Routines.
- **GitHub issue-event trigger filtering is only partially documented**
  (PR/Release mature; issue-opened/labeled listed but undetailed). Treated as
  unverified — the design does not depend on it.
- **Run caps bind:** Max plan = 15 Routine runs/day. One-off scheduled runs do
  not count against the cap.
- Commits/PRs carry my GitHub identity (no service account). Acceptable for a
  solo dev.

## Chosen approach: A — two scheduled batch Routines

Rejected alternatives:
- **B (event-driven, per-issue):** at 5–15 issues/day it exhausts the 15/day
  cap on implementation alone, and depends on the unverified issue-event
  trigger.
- **C (hybrid):** documented as a future upgrade — once issue-event triggers
  are verified, add an event "kick" that runs the implementer early instead of
  waiting for the schedule. Keep batching for review/merge.

Approach A is robust against both unknowns (no auto-merge primitive, unverified
issue trigger) and cap-efficient. The batched reviewer *is* the anti-drowning
layer.

## Architecture & flow

```
[Phone] interactive Claude (Remote Control or claude.ai/code)
   │  describe intent; Claude researches the repo
   │  /issue skill → AskUserQuestion to spec atomically
   ▼
GitHub issue(s)  ── created by MY account, labeled `spec-approved`
   │                (label is the trust token)
   ▼
Routine #1 "implementer" (scheduled, every ~4h, multi-repo)
   gh issue list --label spec-approved --state open
   → oldest N=4 first: branch claude/issue-<n>, implement in-scope,
     test/lint/typecheck, open PR "Closes #<n>", label `auto-impl`
   ▼
Routine #2 "reviewer/merger" (scheduled, +1h after #1, multi-repo)
   gh pr list --label auto-impl --state open
   → six-condition gate:
       ├─ PASS → gh pr merge --squash --auto ; delete branch
       └─ FAIL → label `needs-you` + reason ; collect into one
                 ranked digest → push to phone
```

Two Routines total, both scheduled, both multi-repo. Step 2 is interactive on
the phone because AskUserQuestion can't live in a Routine.

## The provenance trust gate

Fail-closed (anything not positively cleared → escalate, never merge).
Defense-in-depth — a PR auto-merges only if **all six** hold:

1. **Provenance (primary, unforgeable):** PR closes exactly one issue whose
   author is on an allowlist (my account ± known trusted collaborators). A
   stranger's issue fails here regardless of anything else.
2. **Intent token:** that issue carries `spec-approved`. **Only my interactive
   mobile spec'ing session applies this label.** The implementer Routine is
   forbidden from ever touching it — the autonomous side can never mint its own
   trust. Triage access on all target repos is **me only**, so the label is a
   binding gate (not merely an intent marker) — but condition 1 remains primary.
3. **Linkage:** PR body has `Closes #<n>` resolving to an issue passing (1)+(2).
   No resolvable trusted link → escalate.
4. **Risk-path denylist:** diff touches *none* of: auth/crypto/JWT,
   `.github/workflows/**`, IaC/infra, DB migrations, `.env*`/secrets, MTA-STS
   `redirect:"manual"` code, Cloudflare Access policies. Any hit → escalate.
5. **Size envelope:** ≤ ~250 changed lines and ≤ ~8 files (higher-throughput
   setting; tunable). Over → escalate.
6. **CI green + scope-fit:** all required checks pass and changed files fall
   within the issue's declared file-pointer list / "out of scope" section
   (emitted by the `/issue` skill). Drift or red CI → escalate.

**Hard backstop:** branch protection on the default branch (require PR, require
status checks, no force-push, no bypass). Even a buggy reviewer Routine cannot
merge a red or policy-violating PR. Keeps the workflow compliant with the global
"never push to main" rule.

**Documented assumption:** the set of GitHub accounts with *triage* permission
on these repos is just me / known collaborators (true for ~4 solo repos).
Mitigation if that changes: rely on the allowlist-author check (condition 1) as
the binding gate and treat the label purely as an intent marker.

## Routine #1 — Implementer

- `gh issue list --label spec-approved --state open` across configured repos;
  filter to allowlisted authors; **oldest N=6 first** (bounds run length).
- Per issue: branch `claude/issue-<n>`, implement strictly within the issue's
  declared file pointers/scope, honoring that repo's `CLAUDE.md`, `/spec`,
  `/ship` conventions.
- Run tests + lint + typecheck. **On failure: no PR** — comment the failure on
  the issue, add `impl-blocked`, continue.
- On success: open one PR per issue, `Closes #<n>`, conventional-commit title,
  test results in the body, label `auto-impl`.
- **Never** touches `spec-approved`; **never** merges.

## Routine #2 — Reviewer/Merger

- `gh pr list --label auto-impl --state open`; per PR evaluate the six-condition
  gate using `gh` (issue author/labels, `gh pr diff`/files for size+paths,
  `gh pr checks` for CI).
- **PASS →** `gh pr merge --squash --auto`; delete branch; comment outcome on
  the closed issue.
- **FAIL →** label `needs-you` + one-line reason; **skip** PRs already
  `needs-you` (idempotent, never re-merge).
- End of run: assemble *all* escalations into **one ranked digest** (safest/
  smallest first, then issue priority) and deliver as a **Claude mobile app
  notification / session** I open from the phone. This digest is the
  anti-drowning surface — I triage a vetted, ordered list, not raw PRs.

## Failure handling & observability

- Fail-closed everywhere: missing/ambiguous issue link, unparseable diff,
  uncertain scope → escalate, never merge.
- `impl-blocked` items ride along in the reviewer digest.
- "Green" Routine status ≠ task success. **The digest is the source of truth**;
  a missing scheduled digest is the signal to open the session and check.
- Lightweight ledger: reviewer appends a one-line entry (run time, merged
  count, escalated count) to a pinned per-repo tracking issue — a poor-man's
  dashboard, since Routines have no aggregate UI.

## Cap math (Max = 15 runs/day)

- 4 cycles/day × (1 implementer + 1 reviewer) = **8 runs/day**, 7 spare.
- Implementer N=6/run × 4 cycles = up to **24 issues/day** implemented; covers
  5–15/day with headroom, drains backlog oldest-first. (Pilot phase: single
  repo, so effective volume is lower still.)
- One-off scheduled runs don't count against the cap → manual "kick" without
  spending budget.

## Out of scope

- Event-driven per-issue triggering (approach B) — explicitly rejected.
- Cross-repo coordinated changes (each repo is independent).
- Auto-merge of security/auth/infra/migration changes — always escalated.
- Aggregate analytics dashboard — only the lightweight per-repo ledger.

## Settled decisions

- **Digest channel:** Claude mobile app notification/session.
- **Repo access:** owner-only triage → `spec-approved` is a binding gate;
  author-allowlist (condition 1) still primary.
- **Thresholds:** N=6 issues/implementer run; auto-merge envelope ≤250 lines /
  ≤8 files; ~4h cycle (4 cycles/day, ~8 runs/day).
- **Branch protection:** confirmed/provisioned in implementation step 1 before
  any Routine goes live.
- **Rollout:** pilot one repo, then expand to all four.

## Open items for the implementation plan

- **Pilot repo selection** (owner to name). Note: donthype-me's `dev→main` flow
  makes it a poorer first pilot; a `main`-flow repo is simpler to validate.
- Exact `gh` query syntax + multi-repo iteration and per-repo base-branch config
  for both Routines.
- Branch-protection ruleset definition to apply/verify per repo.
- The `/issue` skill output contract the implementer relies on (file pointers,
  out-of-scope section) — confirm/align.
