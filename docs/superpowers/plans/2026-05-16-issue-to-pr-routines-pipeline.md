# Issue → PR → Merge Pipeline (Claude Code Routines) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stand up a two-Routine pipeline for the **dmarcheck** repo where a deterministic, unit-tested provenance/risk gate decides which Claude-authored PRs auto-merge and which escalate to a single ranked Claude-app digest.

**Architecture:** The trust decision is NOT left to Routine prose — it is a pure TypeScript module (`gate-core.ts`) with Vitest unit tests, wrapped by a thin `gh`-calling CLI (`gate.ts`). Routine #1 (implementer, scheduled) turns `spec-approved` issues into PRs; Routine #2 (reviewer/merger, scheduled) runs `gate.ts` per PR and either `gh pr merge --auto`s it or escalates. GitHub branch protection is an independent hard backstop.

**Tech Stack:** TypeScript + Vitest (matches dmarcheck), `gh` CLI, `minimatch` for glob matching, `tsx` to run the gate inside the Routine, Bash for setup scripts, Claude Code Routines (cloud-configured, prompt files are source of truth).

**Reference spec:** `docs/superpowers/specs/2026-05-16-issue-to-pr-routines-pipeline-design.md` (in the hermes-agent repo where this plan also lives).

**Where work happens:** All artifacts are created in the **dmarcheck** repo on a feature branch, merged via PR (never push to main — per global guardrails). This plan document lives in hermes-agent; the engineer executes against a local dmarcheck checkout.

---

## Assumptions (verify in Task 1, do not assume blindly)

- dmarcheck is TypeScript + Vitest with a `package.json`. Task 1 records the **actual** test/lint/typecheck commands; later tasks use the universal fallback `npx vitest run <file>` which works regardless of script wiring.
- `gh` CLI is installed and authenticated as the repo owner (`schmug`).
- Claude Code Routines are configured in the Claude cloud UI/API — there is **no** infrastructure-as-code for the Routine itself. The `.md` prompt files this plan creates are the source of truth; registering a Routine is an explicit, documented manual step (Tasks 9 & 11) using that exact prompt text.
- dmarcheck default/integration branch is `main` (confirmed in spec: dmarcheck is `main`-flow).

---

## File Structure (all paths relative to the dmarcheck repo root)

```
scripts/routine-gate/
  config.ts                 # thresholds, author allowlist, label names, risk-path denylist, per-repo base branch
  gate-core.ts              # PURE functions: provenance, linkage, risk-path, size, scope-fit, aggregate verdict
  github.ts                 # THIN IO: wraps `gh` calls into typed data (integration-tested via pilot, not unit-tested)
  gate.ts                   # CLI entrypoint: gather via github.ts → evaluateGate → JSON verdict + exit code
  __tests__/
    gate-core.test.ts       # Vitest unit tests over the pure functions (fixtures, no network)
scripts/routine-pipeline/
  setup-labels.sh           # creates the 4 pipeline labels on a repo
  setup-branch-protection.sh# audits, then (with --apply) provisions the required default-branch ruleset
  routine-implementer.md    # Routine #1 prompt (source of truth; pasted into the cloud Routine)
  routine-reviewer.md       # Routine #2 prompt (source of truth; invokes gate.ts)
  mobile-speccing.md        # runbook: interactive phone session → /issue → spec-approved
docs/routine-pipeline.md    # operator README: how it fits, cap math, pilot validation log
```

Responsibilities are split so the only file with branching logic (`gate-core.ts`) is pure and fully unit-testable; everything touching the network (`github.ts`) is isolated and thin.

---

## Task 1: Orient in dmarcheck and create the working branch

**Files:** none created yet.

- [ ] **Step 1: Clone/locate dmarcheck and record toolchain**

Run (adjust path if dmarcheck already checked out locally):
```bash
cd ~/src 2>/dev/null || cd ~ ; gh repo clone schmug/dmarcheck 2>/dev/null || true
cd dmarcheck
cat package.json
```
Record from `package.json` the exact values of `scripts.test`, `scripts.lint`, `scripts.typecheck` (or `tsc`). Note them in a scratch note; later tasks refer to them as "the recorded test/lint/typecheck command". If any is missing, the fallbacks are: test → `npx vitest run`, typecheck → `npx tsc --noEmit`, lint → skip if absent.

- [ ] **Step 2: Confirm Vitest is the runner**

Run:
```bash
npx vitest --version
```
Expected: prints a Vitest version. If it fails, STOP and report — the plan's test code targets Vitest.

- [ ] **Step 3: Create the feature branch**

Run:
```bash
git checkout main && git pull
git checkout -b feat/routine-gate-pipeline
```
Expected: now on `feat/routine-gate-pipeline`. (Never commit pipeline work to `main`.)

- [ ] **Step 4: Add runtime dependencies**

Run:
```bash
npm install --save-dev minimatch tsx
```
Expected: `minimatch` and `tsx` appear in `devDependencies`. `minimatch` powers glob matching in the gate; `tsx` lets the Routine run the gate without a build step.

- [ ] **Step 5: Commit the scaffold dependency change**

```bash
git add package.json package-lock.json
git commit -m "chore: add minimatch + tsx for routine gate"
```

---

## Task 2: Gate config module

**Files:**
- Create: `scripts/routine-gate/config.ts`
- Test: `scripts/routine-gate/__tests__/gate-core.test.ts` (config invariants live here too)

- [ ] **Step 1: Write the failing test**

Create `scripts/routine-gate/__tests__/gate-core.test.ts`:
```ts
import { describe, it, expect } from "vitest";
import { CONFIG } from "../config";

describe("CONFIG", () => {
  it("only allowlists the repo owner", () => {
    expect(CONFIG.allowlistAuthors).toEqual(["schmug"]);
  });
  it("uses the higher-throughput envelope", () => {
    expect(CONFIG.size.maxChangedLines).toBe(250);
    expect(CONFIG.size.maxChangedFiles).toBe(8);
    expect(CONFIG.implementerBatch).toBe(6);
  });
  it("has a non-empty risk-path denylist and the four labels", () => {
    expect(CONFIG.riskPathDenylist.length).toBeGreaterThan(5);
    expect(CONFIG.labels).toMatchObject({
      specApproved: "spec-approved",
      autoImpl: "auto-impl",
      needsYou: "needs-you",
      implBlocked: "impl-blocked",
    });
  });
  it("knows dmarcheck is main-flow and donthype-me is dev-flow", () => {
    expect(CONFIG.baseBranchByRepo["dmarcheck"]).toBe("main");
    expect(CONFIG.baseBranchByRepo["donthype-me"]).toBe("dev");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npx vitest run scripts/routine-gate/__tests__/gate-core.test.ts`
Expected: FAIL — cannot resolve `../config`.

- [ ] **Step 3: Write minimal implementation**

Create `scripts/routine-gate/config.ts`:
```ts
export const CONFIG = {
  // Condition 1 (primary, unforgeable): only these issue authors can ever reach auto-merge.
  allowlistAuthors: ["schmug"],

  labels: {
    specApproved: "spec-approved", // minted ONLY by the interactive mobile session
    autoImpl: "auto-impl",         // applied by Routine #1 to PRs it opens
    needsYou: "needs-you",         // applied by Routine #2 on escalation
    implBlocked: "impl-blocked",   // applied by Routine #1 when tests fail
  },

  // Condition 5: higher-throughput envelope (spec: N=6, <=250 lines, <=8 files).
  size: { maxChangedLines: 250, maxChangedFiles: 8 },
  implementerBatch: 6,

  // Per-repo integration branch (spec: donthype-me is dev->main).
  baseBranchByRepo: {
    "dmarcheck": "main",
    "donthype-me": "dev",
    "benchburner": "main",
    "apartment-stager": "main",
  } as Record<string, string>,

  // Condition 4: any match -> escalate, never auto-merge. Globs are minimatch with { dot: true }.
  riskPathDenylist: [
    "**/auth/**", "**/*auth*", "**/crypto/**", "**/*jwt*",
    ".github/workflows/**", "**/migrations/**", "**/*.env*", "**/.dev.vars",
    "**/*mta-sts*", "**/*mta_sts*", "**/*cloudflare*access*",
    "infra/**", "**/terraform/**", "**/*.tf", "wrangler.toml",
  ],
};
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npx vitest run scripts/routine-gate/__tests__/gate-core.test.ts`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/routine-gate/config.ts scripts/routine-gate/__tests__/gate-core.test.ts
git commit -m "feat: routine gate config (allowlist, thresholds, risk denylist)"
```

---

## Task 3: Gate core — provenance + linkage

**Files:**
- Create: `scripts/routine-gate/gate-core.ts`
- Test: `scripts/routine-gate/__tests__/gate-core.test.ts` (append)

- [ ] **Step 1: Write the failing tests** (append to the existing test file)

```ts
import {
  parseClosesIssue, isProvenanceTrusted,
} from "../gate-core";

describe("parseClosesIssue", () => {
  it("extracts the issue number from a Closes line", () => {
    expect(parseClosesIssue("Adds X.\n\nCloses #42")).toBe(42);
  });
  it("is case-insensitive", () => {
    expect(parseClosesIssue("closes #7")).toBe(7);
  });
  it("returns null when absent", () => {
    expect(parseClosesIssue("no link here")).toBeNull();
  });
});

describe("isProvenanceTrusted", () => {
  const cfg = CONFIG;
  it("trusts allowlisted author WITH spec-approved label", () => {
    expect(isProvenanceTrusted({ number: 1, author: "schmug", labels: ["spec-approved"], filePointers: [] }, cfg)).toBe(true);
  });
  it("rejects a stranger even if labelled", () => {
    expect(isProvenanceTrusted({ number: 1, author: "drive-by", labels: ["spec-approved"], filePointers: [] }, cfg)).toBe(false);
  });
  it("rejects allowlisted author WITHOUT the label", () => {
    expect(isProvenanceTrusted({ number: 1, author: "schmug", labels: [], filePointers: [] }, cfg)).toBe(false);
  });
  it("rejects a null issue (fail-closed)", () => {
    expect(isProvenanceTrusted(null, cfg)).toBe(false);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run scripts/routine-gate/__tests__/gate-core.test.ts`
Expected: FAIL — cannot resolve `../gate-core`.

- [ ] **Step 3: Write minimal implementation**

Create `scripts/routine-gate/gate-core.ts`:
```ts
import type { CONFIG as CONFIG_T } from "./config";

export interface IssueInfo {
  number: number;
  author: string;
  labels: string[];
  filePointers: string[]; // glob-ish paths the issue declared as in-scope
}

export interface PrInfo {
  number: number;
  body: string;
  changedFiles: string[];
  additions: number;
  deletions: number;
  ciAllGreen: boolean;
}

type Cfg = typeof CONFIG_T;

export function parseClosesIssue(body: string): number | null {
  const m = body.match(/\bcloses\s+#(\d+)\b/i);
  return m ? Number(m[1]) : null;
}

export function isProvenanceTrusted(issue: IssueInfo | null, cfg: Cfg): boolean {
  if (!issue) return false; // fail-closed
  return (
    cfg.allowlistAuthors.includes(issue.author) &&
    issue.labels.includes(cfg.labels.specApproved)
  );
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `npx vitest run scripts/routine-gate/__tests__/gate-core.test.ts`
Expected: PASS (config + parseClosesIssue + isProvenanceTrusted suites).

- [ ] **Step 5: Commit**

```bash
git add scripts/routine-gate/gate-core.ts scripts/routine-gate/__tests__/gate-core.test.ts
git commit -m "feat: gate core provenance + Closes-link parsing"
```

---

## Task 4: Gate core — risk-path, size, scope-fit

**Files:**
- Modify: `scripts/routine-gate/gate-core.ts`
- Test: `scripts/routine-gate/__tests__/gate-core.test.ts` (append)

- [ ] **Step 1: Write the failing tests** (append)

```ts
import { touchesRiskPath, withinSizeEnvelope, scopeDrift } from "../gate-core";

describe("touchesRiskPath", () => {
  it("flags a workflow file", () => {
    expect(touchesRiskPath([".github/workflows/ci.yml"], CONFIG.riskPathDenylist))
      .toEqual([".github/workflows/ci.yml"]);
  });
  it("flags an mta-sts source file", () => {
    expect(touchesRiskPath(["src/mta-sts-fetch.ts"], CONFIG.riskPathDenylist))
      .toEqual(["src/mta-sts-fetch.ts"]);
  });
  it("passes an ordinary source file", () => {
    expect(touchesRiskPath(["src/analyzers/spf.ts"], CONFIG.riskPathDenylist)).toEqual([]);
  });
});

describe("withinSizeEnvelope", () => {
  const base = { number: 1, body: "", changedFiles: ["a.ts"], ciAllGreen: true };
  it("accepts a small diff", () => {
    expect(withinSizeEnvelope({ ...base, additions: 100, deletions: 40 }, CONFIG)).toBe(true);
  });
  it("rejects too many lines", () => {
    expect(withinSizeEnvelope({ ...base, additions: 300, deletions: 0 }, CONFIG)).toBe(false);
  });
  it("rejects too many files", () => {
    expect(withinSizeEnvelope(
      { ...base, additions: 10, deletions: 0, changedFiles: Array(9).fill("x.ts") }, CONFIG)).toBe(false);
  });
});

describe("scopeDrift", () => {
  it("returns files outside declared pointers", () => {
    expect(scopeDrift(["src/a.ts", "src/b.ts"], ["src/a.ts"])).toEqual(["src/b.ts"]);
  });
  it("treats empty pointers as total drift (fail-closed)", () => {
    expect(scopeDrift(["src/a.ts"], [])).toEqual(["src/a.ts"]);
  });
  it("matches glob pointers", () => {
    expect(scopeDrift(["src/analyzers/spf.ts"], ["src/analyzers/**"])).toEqual([]);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run scripts/routine-gate/__tests__/gate-core.test.ts`
Expected: FAIL — `touchesRiskPath`/`withinSizeEnvelope`/`scopeDrift` not exported.

- [ ] **Step 3: Write minimal implementation** (append to `gate-core.ts`)

```ts
import { minimatch } from "minimatch";

export function touchesRiskPath(files: string[], denylist: string[]): string[] {
  return files.filter((f) => denylist.some((p) => minimatch(f, p, { dot: true })));
}

export function withinSizeEnvelope(pr: PrInfo, cfg: Cfg): boolean {
  return (
    pr.additions + pr.deletions <= cfg.size.maxChangedLines &&
    pr.changedFiles.length <= cfg.size.maxChangedFiles
  );
}

// Returns changed files NOT covered by the issue's declared pointers.
// Empty pointers => every file is drift (fail-closed: no declared scope = not safe).
export function scopeDrift(changedFiles: string[], pointers: string[]): string[] {
  if (pointers.length === 0) return [...changedFiles];
  return changedFiles.filter((f) => !pointers.some((p) => minimatch(f, p, { dot: true })));
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `npx vitest run scripts/routine-gate/__tests__/gate-core.test.ts`
Expected: PASS (all suites so far).

- [ ] **Step 5: Commit**

```bash
git add scripts/routine-gate/gate-core.ts scripts/routine-gate/__tests__/gate-core.test.ts
git commit -m "feat: gate core risk-path, size envelope, scope-drift checks"
```

---

## Task 5: Gate core — aggregate verdict (the six-condition decision)

**Files:**
- Modify: `scripts/routine-gate/gate-core.ts`
- Test: `scripts/routine-gate/__tests__/gate-core.test.ts` (append)

- [ ] **Step 1: Write the failing tests** (append)

```ts
import { evaluateGate, type GateInput } from "../gate-core";

function baseInput(): GateInput {
  return {
    cfg: CONFIG,
    issue: { number: 42, author: "schmug", labels: ["spec-approved"], filePointers: ["src/analyzers/**"] },
    pr: {
      number: 100,
      body: "Implements analyzer tweak.\n\nCloses #42",
      changedFiles: ["src/analyzers/spf.ts"],
      additions: 30, deletions: 5, ciAllGreen: true,
    },
  };
}

describe("evaluateGate", () => {
  it("PASSES a trusted, small, in-scope, green PR", () => {
    const v = evaluateGate(baseInput());
    expect(v.pass).toBe(true);
    expect(v.reasons).toEqual([]);
  });
  it("FAILS a stranger's issue (provenance)", () => {
    const i = baseInput(); i.issue!.author = "drive-by";
    const v = evaluateGate(i);
    expect(v.pass).toBe(false);
    expect(v.reasons.join(" ")).toMatch(/not on allowlist/);
  });
  it("FAILS when no Closes link", () => {
    const i = baseInput(); i.pr.body = "no link";
    const v = evaluateGate(i);
    expect(v.pass).toBe(false);
    expect(v.reasons.join(" ")).toMatch(/Closes/);
  });
  it("FAILS when linked issue is missing (fail-closed)", () => {
    const i = baseInput(); i.issue = null;
    const v = evaluateGate(i);
    expect(v.pass).toBe(false);
  });
  it("FAILS on risk path even if everything else is fine", () => {
    const i = baseInput(); i.pr.changedFiles = [".github/workflows/ci.yml"]; i.issue!.filePointers = [".github/workflows/**"];
    const v = evaluateGate(i);
    expect(v.pass).toBe(false);
    expect(v.reasons.join(" ")).toMatch(/risk-path/);
  });
  it("FAILS on oversize diff", () => {
    const i = baseInput(); i.pr.additions = 500;
    expect(evaluateGate(i).pass).toBe(false);
  });
  it("FAILS on scope drift", () => {
    const i = baseInput(); i.pr.changedFiles = ["src/unrelated.ts"];
    const v = evaluateGate(i);
    expect(v.pass).toBe(false);
    expect(v.reasons.join(" ")).toMatch(/scope drift/);
  });
  it("FAILS on red CI", () => {
    const i = baseInput(); i.pr.ciAllGreen = false;
    expect(evaluateGate(i).pass).toBe(false);
  });
  it("FAILS when PR closes a different issue than evaluated", () => {
    const i = baseInput(); i.pr.body = "Closes #999";
    const v = evaluateGate(i);
    expect(v.pass).toBe(false);
    expect(v.reasons.join(" ")).toMatch(/#999/);
  });
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `npx vitest run scripts/routine-gate/__tests__/gate-core.test.ts`
Expected: FAIL — `evaluateGate` / `GateInput` not exported.

- [ ] **Step 3: Write minimal implementation** (append to `gate-core.ts`)

```ts
export interface GateInput {
  cfg: Cfg;
  issue: IssueInfo | null;
  pr: PrInfo;
}

export interface GateVerdict {
  pass: boolean;
  reasons: string[]; // empty iff pass === true
}

export function evaluateGate(input: GateInput): GateVerdict {
  const { cfg, issue, pr } = input;
  const reasons: string[] = [];

  // Condition 3: linkage
  const closes = parseClosesIssue(pr.body);
  if (closes === null) reasons.push("PR body has no `Closes #n` link");

  // Conditions 1 + 2: provenance + intent token
  if (!issue) {
    reasons.push("linked issue not found / unreadable (fail-closed)");
  } else {
    if (closes !== null && closes !== issue.number) {
      reasons.push(`PR closes #${closes} but evaluated issue is #${issue.number}`);
    }
    if (!cfg.allowlistAuthors.includes(issue.author)) {
      reasons.push(`issue author @${issue.author} not on allowlist`);
    }
    if (!issue.labels.includes(cfg.labels.specApproved)) {
      reasons.push(`issue #${issue.number} missing "${cfg.labels.specApproved}" label`);
    }
  }

  // Condition 4: risk-path denylist
  const risky = touchesRiskPath(pr.changedFiles, cfg.riskPathDenylist);
  if (risky.length) reasons.push(`touches risk path(s): ${risky.join(", ")}`);

  // Condition 5: size envelope
  if (!withinSizeEnvelope(pr, cfg)) {
    reasons.push(
      `exceeds size envelope (${pr.additions + pr.deletions} lines / ${pr.changedFiles.length} files; ` +
        `max ${cfg.size.maxChangedLines}/${cfg.size.maxChangedFiles})`,
    );
  }

  // Condition 6a: scope-fit
  const drift = issue ? scopeDrift(pr.changedFiles, issue.filePointers) : [...pr.changedFiles];
  if (drift.length) reasons.push(`scope drift outside declared pointers: ${drift.join(", ")}`);

  // Condition 6b: CI
  if (!pr.ciAllGreen) reasons.push("CI not green");

  return { pass: reasons.length === 0, reasons };
}
```

- [ ] **Step 4: Run to verify it passes**

Run: `npx vitest run scripts/routine-gate/__tests__/gate-core.test.ts`
Expected: PASS (all suites; ~26 assertions).

- [ ] **Step 5: Run the full repo test suite (no regressions)**

Run the recorded test command (fallback: `npx vitest run`).
Expected: dmarcheck's existing suite still green + the new gate-core suite.

- [ ] **Step 6: Commit**

```bash
git add scripts/routine-gate/gate-core.ts scripts/routine-gate/__tests__/gate-core.test.ts
git commit -m "feat: aggregate six-condition gate verdict (fail-closed)"
```

---

## Task 6: GitHub IO layer (thin, integration-tested only)

**Files:**
- Create: `scripts/routine-gate/github.ts`

> This file is intentionally NOT unit-tested: it is pure I/O over the `gh` CLI with no branching logic. It is exercised for real in the Task 12 pilot validation. Keeping logic out of here is what makes that acceptable.

- [ ] **Step 1: Write the implementation**

Create `scripts/routine-gate/github.ts`:
```ts
import { execFileSync } from "node:child_process";
import type { IssueInfo, PrInfo } from "./gate-core";

function gh(args: string[]): string {
  return execFileSync("gh", args, { encoding: "utf8", maxBuffer: 10 * 1024 * 1024 });
}

export function fetchPr(repo: string, pr: number): PrInfo {
  const j = JSON.parse(
    gh(["pr", "view", String(pr), "--repo", repo, "--json",
      "body,additions,deletions,files,statusCheckRollup"]),
  );
  const changedFiles: string[] = (j.files ?? []).map((f: any) => f.path);
  const rollup: any[] = j.statusCheckRollup ?? [];
  const ciAllGreen =
    rollup.length > 0 &&
    rollup.every((c: any) =>
      (c.conclusion ?? c.state) === "SUCCESS" || (c.conclusion ?? c.state) === "NEUTRAL");
  return {
    number: pr,
    body: j.body ?? "",
    changedFiles,
    additions: j.additions ?? 0,
    deletions: j.deletions ?? 0,
    ciAllGreen,
  };
}

// filePointers come from a fenced block in the issue body the /issue skill emits:
//   ```scope
//   src/analyzers/**
//   src/shared/scoring.ts
//   ```
export function fetchIssue(repo: string, num: number): IssueInfo | null {
  let j: any;
  try {
    j = JSON.parse(
      gh(["issue", "view", String(num), "--repo", repo, "--json",
        "number,author,labels,body"]),
    );
  } catch {
    return null; // fail-closed: unreadable issue
  }
  const body: string = j.body ?? "";
  const m = body.match(/```scope\s*([\s\S]*?)```/i);
  const filePointers = m
    ? m[1].split("\n").map((s) => s.trim()).filter(Boolean)
    : [];
  return {
    number: j.number,
    author: j.author?.login ?? "",
    labels: (j.labels ?? []).map((l: any) => l.name),
    filePointers,
  };
}
```

- [ ] **Step 2: Typecheck**

Run the recorded typecheck command (fallback: `npx tsc --noEmit`).
Expected: no type errors in `scripts/routine-gate/`.

- [ ] **Step 3: Commit**

```bash
git add scripts/routine-gate/github.ts
git commit -m "feat: thin gh IO layer for routine gate"
```

---

## Task 7: Gate CLI entrypoint

**Files:**
- Create: `scripts/routine-gate/gate.ts`

- [ ] **Step 1: Write the implementation**

Create `scripts/routine-gate/gate.ts`:
```ts
#!/usr/bin/env -S npx tsx
import { CONFIG } from "./config";
import { evaluateGate, parseClosesIssue } from "./gate-core";
import { fetchPr, fetchIssue } from "./github";

// Usage: npx tsx scripts/routine-gate/gate.ts --repo owner/name --pr 123
function arg(flag: string): string {
  const i = process.argv.indexOf(flag);
  if (i === -1 || i + 1 >= process.argv.length) {
    console.error(`missing ${flag}`);
    process.exit(2);
  }
  return process.argv[i + 1];
}

const repo = arg("--repo");
const prNum = Number(arg("--pr"));

const pr = fetchPr(repo, prNum);
const closes = parseClosesIssue(pr.body);
const issue = closes !== null ? fetchIssue(repo, closes) : null;

const verdict = evaluateGate({ cfg: CONFIG, issue, pr });

// Machine-readable for the Routine to parse.
console.log(JSON.stringify({ repo, pr: prNum, ...verdict }, null, 2));

// Exit code is the contract the Routine keys on: 0 = auto-merge, 2 = escalate.
process.exit(verdict.pass ? 0 : 2);
```

- [ ] **Step 2: Make it executable + typecheck**

Run:
```bash
chmod +x scripts/routine-gate/gate.ts
```
Then the recorded typecheck command (fallback: `npx tsc --noEmit`). Expected: clean.

- [ ] **Step 3: Smoke-run against a known PR (manual sanity, not a unit test)**

Run (replace 1 with any real merged/old PR number in dmarcheck):
```bash
npx tsx scripts/routine-gate/gate.ts --repo schmug/dmarcheck --pr 1 ; echo "exit=$?"
```
Expected: prints a JSON verdict object and an `exit=` line (0 or 2). Any crash/stack trace = bug to fix before commit.

- [ ] **Step 4: Commit**

```bash
git add scripts/routine-gate/gate.ts
git commit -m "feat: gate CLI entrypoint (JSON verdict + exit-code contract)"
```

---

## Task 8: Pipeline labels setup script

**Files:**
- Create: `scripts/routine-pipeline/setup-labels.sh`

- [ ] **Step 1: Write the script**

Create `scripts/routine-pipeline/setup-labels.sh`:
```bash
#!/usr/bin/env bash
# Creates the 4 pipeline labels on a repo (idempotent).
# Usage: scripts/routine-pipeline/setup-labels.sh owner/name
set -euo pipefail
REPO="${1:?usage: setup-labels.sh owner/name}"

create() { # name color description
  gh label create "$1" --repo "$REPO" --color "$2" --description "$3" --force
}
create "spec-approved" "0E8A16" "Issue spec'd by owner in interactive session; trust token for auto-merge"
create "auto-impl"     "1D76DB" "PR opened by the implementer Routine"
create "needs-you"     "D93F0B" "Escalated by the reviewer Routine; needs owner decision"
create "impl-blocked"  "B60205" "Implementer Routine could not produce a green PR"
echo "labels ensured on $REPO"
```

- [ ] **Step 2: Run it against dmarcheck**

Run:
```bash
chmod +x scripts/routine-pipeline/setup-labels.sh
scripts/routine-pipeline/setup-labels.sh schmug/dmarcheck
```
Expected: `labels ensured on schmug/dmarcheck`. Verify with `gh label list --repo schmug/dmarcheck | grep -E 'spec-approved|auto-impl|needs-you|impl-blocked'`.

- [ ] **Step 3: Commit**

```bash
git add scripts/routine-pipeline/setup-labels.sh
git commit -m "feat: pipeline label setup script"
```

---

## Task 9: Branch-protection backstop script (audit, then --apply)

**Files:**
- Create: `scripts/routine-pipeline/setup-branch-protection.sh`

> Spec decision was "confirm first": this script **audits and prints** by default and only mutates with an explicit `--apply` flag.

- [ ] **Step 1: Write the script**

Create `scripts/routine-pipeline/setup-branch-protection.sh`:
```bash
#!/usr/bin/env bash
# Audits (default) or provisions (--apply) the required default-branch ruleset.
# Required: PRs only, >=1 approving review OR required status checks, no force-push.
# Usage: setup-branch-protection.sh owner/name [branch] [--apply]
set -euo pipefail
REPO="${1:?usage: setup-branch-protection.sh owner/name [branch] [--apply]}"
BRANCH="${2:-main}"
APPLY="${3:-}"

echo "== current protection for $REPO@$BRANCH =="
gh api "repos/$REPO/branches/$BRANCH/protection" 2>/dev/null \
  | jq '{required_status_checks, enforce_admins, required_pull_request_reviews, allow_force_pushes}' \
  || echo "(no protection set)"

if [[ "$APPLY" != "--apply" ]]; then
  echo
  echo "DRY RUN. Re-run with --apply as the 3rd arg to provision the ruleset below:"
  echo "  - require a pull request before merging"
  echo "  - require status checks to pass (strict)"
  echo "  - block force pushes"
  exit 0
fi

gh api -X PUT "repos/$REPO/branches/$BRANCH/protection" \
  --input - <<'JSON'
{
  "required_status_checks": { "strict": true, "contexts": [] },
  "enforce_admins": false,
  "required_pull_request_reviews": { "required_approving_review_count": 0 },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false
}
JSON
echo "protection applied to $REPO@$BRANCH"
```

> Note: `required_approving_review_count: 0` is deliberate — auto-merge of trusted PRs is the whole point; the *gate* + status checks are the control, not a human approval count. Branch protection's job here is "no red merges, no force-push to main", consistent with the global guardrail.

- [ ] **Step 2: Run the audit (no mutation)**

Run:
```bash
chmod +x scripts/routine-pipeline/setup-branch-protection.sh
scripts/routine-pipeline/setup-branch-protection.sh schmug/dmarcheck main
```
Expected: prints current protection (or "(no protection set)") and a DRY RUN notice. **Do not run with `--apply` yet** — that is a Task 12 go-live step the owner confirms.

- [ ] **Step 3: Commit**

```bash
git add scripts/routine-pipeline/setup-branch-protection.sh
git commit -m "feat: branch-protection audit/apply backstop script"
```

---

## Task 10: Routine #1 prompt — implementer

**Files:**
- Create: `scripts/routine-pipeline/routine-implementer.md`

- [ ] **Step 1: Write the prompt file**

Create `scripts/routine-pipeline/routine-implementer.md`:
```markdown
# Routine: Implementer (scheduled, every ~4h)

You are the implementer for the issue→PR pipeline. The repo is checked out at the
working directory. Do exactly this:

1. List candidate issues:
   `gh issue list --repo <REPO> --label spec-approved --state open --json number,author,createdAt`
2. Discard any whose `author.login` is not `schmug`. Sort the rest oldest-first.
   Take at most the first **6**.
3. For EACH selected issue #N (one at a time, fully, before the next):
   a. `git checkout <base>` (base = `main` for dmarcheck) `&& git pull`.
   b. `git checkout -b claude/issue-N`.
   c. Read issue #N. Implement it **strictly within** the file pointers in its
      ```scope``` block and its acceptance criteria. Honor the repo's CLAUDE.md,
      /spec and /ship conventions. Do NOT touch anything outside declared scope.
   d. Run the repo's tests, lint, and typecheck.
   e. If anything fails and you cannot fix it within scope: do NOT open a PR.
      Comment the failure on issue #N, add the `impl-blocked` label, move to the
      next issue.
   f. If green: open ONE PR with `gh pr create`, base = `main`, body MUST contain
      `Closes #N`, a conventional-commit title (`feat:`/`fix:`/etc.), and the
      test results. Add the `auto-impl` label to the PR.
4. NEVER add, remove, or modify the `spec-approved` label anywhere.
5. NEVER merge anything. Your job ends at "PR opened" or "impl-blocked".
6. End your run with a one-line summary: issues processed, PRs opened, blocked.
```

- [ ] **Step 2: Commit**

```bash
git add scripts/routine-pipeline/routine-implementer.md
git commit -m "feat: implementer Routine prompt"
```

---

## Task 11: Routine #2 prompt — reviewer/merger (wires the gate)

**Files:**
- Create: `scripts/routine-pipeline/routine-reviewer.md`

- [ ] **Step 1: Write the prompt file**

Create `scripts/routine-pipeline/routine-reviewer.md`:
```markdown
# Routine: Reviewer / Merger (scheduled, +1h after implementer)

You are the reviewer/merger. The repo is checked out at the working directory.
The gate is a deterministic script — TRUST ITS EXIT CODE, do not re-judge.

1. `gh pr list --repo <REPO> --label auto-impl --state open --json number,labels`
2. Skip any PR that already has the `needs-you` label (idempotent).
3. For EACH remaining PR #P:
   a. Run: `npx tsx scripts/routine-gate/gate.ts --repo <REPO> --pr P`
   b. Capture stdout (JSON verdict) and the exit code.
   c. If exit code == 0 (PASS):
      `gh pr merge P --repo <REPO> --squash --auto --delete-branch`
      Then comment the one-line outcome on the issue the PR closes.
   d. If exit code == 2 (FAIL): add the `needs-you` label to PR #P and add a PR
      comment containing the `reasons` array from the JSON verdict.
   e. Any other exit code / crash: treat as FAIL (fail-closed) — apply
      `needs-you`, comment "gate errored: <stderr>". Never merge on error.
4. Build ONE digest of every PR you escalated this run, ranked by: smallest
   diff first, then issue priority labels if present. Each line:
   `#P <title> — <top reason> — <url>`. Also append any `impl-blocked` issues.
5. Deliver the digest as the final message of the run (this surfaces as the
   Claude mobile app notification/session). If nothing escalated, say
   "All clear: <K> auto-merged, 0 escalations."
6. Append one ledger line to the pinned `Routine pipeline ledger` issue:
   `<ISO time> — merged <K>, escalated <M>, blocked <B>`.
```

- [ ] **Step 2: Create the ledger issue (one-time)**

Run:
```bash
gh issue create --repo schmug/dmarcheck \
  --title "Routine pipeline ledger" \
  --body "Append-only run ledger written by the reviewer Routine. Do not close." \
  --label "auto-impl" 2>/dev/null || true
gh issue list --repo schmug/dmarcheck --search "Routine pipeline ledger in:title"
```
Expected: the ledger issue exists (note its number; pin it via the GitHub UI). Then remove the mislabel: `gh issue edit <num> --repo schmug/dmarcheck --remove-label auto-impl`.

- [ ] **Step 3: Commit**

```bash
git add scripts/routine-pipeline/routine-reviewer.md
git commit -m "feat: reviewer/merger Routine prompt wiring the deterministic gate"
```

---

## Task 12: Mobile spec'ing runbook + operator README

**Files:**
- Create: `scripts/routine-pipeline/mobile-speccing.md`
- Create: `docs/routine-pipeline.md`

- [ ] **Step 1: Write the mobile runbook**

Create `scripts/routine-pipeline/mobile-speccing.md`:
```markdown
# Mobile spec'ing runbook (the interactive half — Step 2 of the pipeline)

AskUserQuestion CANNOT run inside a Routine, so issue creation is interactive.

From the phone:
1. Open a session via Remote Control (`claude` on an always-on machine, steered
   from the Claude mobile app) OR claude.ai/code.
2. Describe the intent. Ask Claude to research the repo and propose
   atomically-sized issues.
3. Run the `/issue` skill. It must produce, for each issue:
   - a task-first body usable as a cold prompt,
   - an explicit ```scope``` fenced block listing in-scope file globs/paths,
   - an "Out of scope" section,
   - acceptance criteria.
   Use AskUserQuestion to tighten scope until each issue is one atomic change.
4. Create each issue on `schmug/dmarcheck` and apply the `spec-approved` label
   YOURSELF in this session (this is the trust token; the implementer Routine
   can never apply it).
5. Done. The scheduled implementer Routine will pick them up within ~4h (or
   trigger a one-off run manually; one-off runs don't count against the cap).
```

- [ ] **Step 2: Write the operator README**

Create `docs/routine-pipeline.md`:
```markdown
# Routine pipeline — operator guide

## Flow
Phone (interactive, /issue + spec-approved) → Routine #1 implementer (scheduled
~4h, batch 6, opens `claude/issue-N` PRs labelled `auto-impl`) → Routine #2
reviewer (+1h, runs `scripts/routine-gate/gate.ts` per PR, exit 0 = squash-merge,
exit 2 = `needs-you` + digest) → Claude-app digest for the rest.

## Trust gate (deterministic, scripts/routine-gate/)
Six conditions, fail-closed, all must hold to auto-merge: (1) issue author in
allowlist, (2) `spec-approved` label, (3) resolvable `Closes #N`, (4) no
risk-path hit, (5) ≤250 lines/≤8 files, (6) CI green + no scope drift. Branch
protection is the independent backstop.

## Cap math (Max = 15 Routine runs/day)
4 cycles/day × (implementer + reviewer) = 8 runs/day. Implementer 6 issues/run ×
4 = up to 24 issues/day. One-off scheduled runs don't count against the cap.

## Registering the Routines (manual cloud step — no IaC for Routines)
For each of routine-implementer.md and routine-reviewer.md: in claude.ai/code →
Routines, create a scheduled routine, bind repo `schmug/dmarcheck`, paste the
prompt file contents, set schedule (implementer every 4h; reviewer offset +1h),
enable `claude/`-branch pushes for the implementer. Routine commits appear as
`schmug`.

## Pilot validation log
Filled in during Task 12 go-live. Scenario → expected → actual.
```

- [ ] **Step 3: Commit**

```bash
git add scripts/routine-pipeline/mobile-speccing.md docs/routine-pipeline.md
git commit -m "docs: mobile spec'ing runbook + operator guide"
```

---

## Task 13: Open the PR into dmarcheck

**Files:** none.

- [ ] **Step 1: Run the full suite + typecheck one last time**

Run the recorded test command and typecheck (fallbacks `npx vitest run` / `npx tsc --noEmit`).
Expected: all green. Report counts explicitly (e.g. "N passing, 0 failing").

- [ ] **Step 2: Push and open the PR**

```bash
git push -u origin feat/routine-gate-pipeline
gh pr create --repo schmug/dmarcheck --base main \
  --title "feat: issue→PR→merge Routine pipeline + deterministic gate" \
  --body "Implements docs/routine-pipeline.md. Gate is unit-tested (scripts/routine-gate/__tests__). Routine prompts + setup scripts included. Branch protection NOT yet applied; Routines NOT yet registered — see go-live (Task 12 of the plan)."
```
Expected: PR URL printed. (PR, not direct push — per global guardrails.)

---

## Task 14: Go-live + pilot validation (owner-gated)

> These steps mutate GitHub / register cloud Routines. Do them only after the Task 13 PR is merged and the owner explicitly says go.

- [ ] **Step 1: Apply branch protection**

```bash
scripts/routine-pipeline/setup-branch-protection.sh schmug/dmarcheck main --apply
scripts/routine-pipeline/setup-branch-protection.sh schmug/dmarcheck main   # re-audit, confirm applied
```
Expected: protection now shows required status checks + force-push blocked.

- [ ] **Step 2: Register both Routines** per `docs/routine-pipeline.md` "Registering the Routines". Verify each appears in claude.ai/code → Routines bound to `schmug/dmarcheck`.

- [ ] **Step 3: Run the 5 pilot validation scenarios**

For each, create the input and record actual vs expected in `docs/routine-pipeline.md` "Pilot validation log":

| # | Setup | Expected |
|---|-------|----------|
| V1 | Issue from a non-`schmug` account (or simulate by omitting `spec-approved`), implemented | Reviewer escalates, NEVER auto-merges (provenance fail-closed) |
| V2 | Small in-scope `spec-approved` issue from `schmug` | Auto-merged by reviewer; branch deleted |
| V3 | `spec-approved` issue whose impl touches `src/*mta-sts*` or `.github/workflows/**` | Escalated (risk-path), digest delivered to Claude app |
| V4 | `spec-approved` issue whose diff exceeds 250 lines / 8 files | Escalated (size envelope) |
| V5 | A PR with a deliberately failing check | Escalated; branch protection also blocks the merge |

- [ ] **Step 4: Commit the filled validation log**

```bash
git checkout -b chore/pilot-validation-log
git add docs/routine-pipeline.md
git commit -m "docs: dmarcheck pilot validation results"
git push -u origin chore/pilot-validation-log
gh pr create --repo schmug/dmarcheck --base main \
  --title "docs: routine pipeline pilot validation results" --fill
```

- [ ] **Step 5: Decide expansion**

If all 5 scenarios pass, file a follow-up issue to expand to donthype-me (remember its base branch is `dev`), benchburner, apartment-stager — reusing the same scripts with `CONFIG.baseBranchByRepo`.

---

## Self-Review (completed by plan author)

**1. Spec coverage:**
- Architecture / two scheduled Routines → Tasks 10, 11, 14.
- Six-condition trust gate → Tasks 2–5 (pure + tested), 6–7 (wiring).
- Provenance primary + label binding (owner-only triage) → Task 2 config + Task 3 + Task 5 tests.
- Risk-path denylist incl. MTA-STS / workflows / secrets → Task 2 + Task 4 + V3.
- Size envelope N=6 / ≤250 / ≤8 → Task 2 + Task 4 + V4.
- CI green + scope-fit → Task 5 + Task 6 (`statusCheckRollup`, ```scope``` parsing).
- Branch-protection backstop, "confirm first" → Task 9 (dry-run default) + Task 14.
- Per-repo base branch (donthype-me=dev) → Task 2 config + Task 14 Step 5.
- Digest via Claude app → Task 11 Step 5.
- Observability ledger → Task 11 Step 2/6 + README.
- Cap math → README (Task 12).
- Mobile spec'ing / `/issue` contract → Task 12 runbook + `github.ts` ```scope``` parser.
- Pilot on dmarcheck → Tasks 8–14 all target `schmug/dmarcheck`.

**2. Placeholder scan:** No TBD/TODO. Discovery (Task 1) records concrete commands with concrete fallbacks (`npx vitest run`, `npx tsc --noEmit`) — not placeholders. Routine registration is an explicit manual step with exact prompt content provided, not "configure later".

**3. Type consistency:** `IssueInfo`/`PrInfo`/`GateInput`/`GateVerdict` defined in Task 3/5 (`gate-core.ts`) and consumed unchanged in Tasks 6 (`github.ts`) and 7 (`gate.ts`). `evaluateGate` exit-code contract (0/2) defined in Task 7 and consumed verbatim in Task 11 reviewer prompt. Label names centralized in `CONFIG.labels` and referenced symbolically everywhere.
