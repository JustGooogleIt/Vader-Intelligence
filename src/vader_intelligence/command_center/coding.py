"""Maintenance (coding-improvement) runs, J4: one deterministic coordinator (this
service), at most two writers and one independent reviewer, under the global cap.

A run freezes its task brief (issue, acceptance criteria, allowed checks) before any
execution; the writer works in its own git worktree and branch; the controller runs the
trusted checks against the exact commit; a reviewer that is never the writer re-checks
the same commit in a fresh worktree and applies the policy review; a review packet and
an accept/reject recommendation are produced. Nothing merges.

Executors: `scripted` applies an operator-supplied patch (labelled **simulated writer**:
it establishes the pipeline, not an agent); `claude_cli` and `codex_cli` build the exact
argv from the inspected CLIs but refuse to run unless an authorization record in the root
names established, $0-incremental usage. Private worktrees and process groups limit
accidents; they are not an OS security sandbox.
"""

import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from ..provenance import source_hash
from .store import TERMINAL_RUN_STATES, new_id

EXECUTORS = ("scripted", "claude_cli", "codex_cli")
AUTHORIZATION_FILE = "authorization.json"
MAX_WRITERS = 2
MAX_PATCH_BYTES = 200 * 1024
LOG_TAIL = 4000
DEFAULT_PROTECTED = (
    "docs/evidence/",
    "src/vader_intelligence/pods/evaluator.py",
    "src/vader_intelligence/research/registry.py",
    "src/vader_intelligence/evaluation/scoring.py",
    "src/vader_intelligence/command_center/coding.py",
    "uv.lock",
    "pyproject.toml",
)
CHECKS = {
    "ruff_check": lambda paths: ["-m", "ruff", "check", *(paths or ["src", "tests"])],
    "ruff_format": lambda paths: ["-m", "ruff", "format", "--check", *(paths or ["src", "tests"])],
    "pytest": lambda paths: ["-m", "pytest", "-q", "-p", "no:cacheprovider", *(paths or ["tests"])],
}
SAFE_PATH = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,200}\Z")
ACTIVE_STATES = ("created", "running", "paused", "canceling")


def validate_brief(payload):
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    issue = payload.get("issue")
    if not isinstance(issue, str) or not 1 <= len(issue.strip()) <= 200:
        raise ValueError("issue must be 1..200 characters")
    description = payload.get("description", "")
    if not isinstance(description, str) or len(description) > 4000:
        raise ValueError("description too long")
    acceptance = payload.get("acceptance_criteria")
    if (
        not isinstance(acceptance, list)
        or not 1 <= len(acceptance) <= 10
        or not all(isinstance(a, str) and a.strip() for a in acceptance)
    ):
        raise ValueError("acceptance_criteria must be 1..10 non-empty strings")
    executor = payload.get("executor", "scripted")
    if executor not in EXECUTORS:
        raise ValueError(f"unknown executor {executor!r}")
    patch = payload.get("patch")
    if executor == "scripted":
        if not isinstance(patch, str) or not patch.strip() or len(patch.encode()) > MAX_PATCH_BYTES:
            raise ValueError("scripted executor needs a non-empty unified diff under 200 KiB")
    checks = payload.get("checks") or [
        {"name": "ruff_check"},
        {"name": "ruff_format"},
        {"name": "pytest"},
    ]
    normalized = []
    for check in checks:
        if not isinstance(check, dict) or check.get("name") not in CHECKS:
            raise ValueError(f"checks may only name {sorted(CHECKS)}")
        paths = check.get("paths") or []
        if (
            not isinstance(paths, list)
            or len(paths) > 20
            or not all(
                isinstance(p, str) and SAFE_PATH.fullmatch(p) and ".." not in p for p in paths
            )
        ):
            raise ValueError("check paths must be repo-relative and plain")
        normalized.append({"name": check["name"], "paths": paths})
    limits = dict(payload.get("limits") or {})
    wall = limits.get("wall_seconds", 1800)
    if not isinstance(wall, int) or isinstance(wall, bool) or not 30 <= wall <= 3600:
        raise ValueError("limits.wall_seconds must be an integer in 30..3600")
    protected = tuple(payload.get("protected_paths") or DEFAULT_PROTECTED)
    if not all(isinstance(p, str) and SAFE_PATH.fullmatch(p) for p in protected):
        raise ValueError("protected paths must be plain repo-relative prefixes")
    brief = {
        "kind": "maintenance",
        "issue": issue.strip(),
        "description": description,
        "acceptance_criteria": [a.strip() for a in acceptance],
        "executor": executor,
        "simulated": executor == "scripted",
        "checks": normalized,
        "limits": {"wall_seconds": wall},
        "protected_paths": list(protected),
        "patch_sha256": hashlib.sha256(patch.encode()).hexdigest()
        if isinstance(patch, str)
        else None,
        "prompt": payload.get("prompt")
        if isinstance(payload.get("prompt"), str) and len(payload.get("prompt")) <= 8000
        else None,
    }
    brief["brief_sha256"] = hashlib.sha256(
        json.dumps(brief, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return brief, patch


def authorization(root):
    """The operator's record of established, $0-incremental agent usage, or None."""
    path = Path(root) / AUTHORIZATION_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = (
        isinstance(data, dict)
        and data.get("usage") == "established"
        and data.get("incremental_cost_usd") == 0
        and isinstance(data.get("executors"), list)
        and isinstance(data.get("by"), str)
        and isinstance(data.get("at"), str)
    )
    return data if ok else None


def agent_argv(executor, worktree, prompt, output_path):
    """Exact argv for a real coding agent, from the inspected CLIs (never guessed flags).
    Claude Code 2.1.309: `-p` print mode, `--permission-mode`, `--allowedTools`,
    `--max-budget-usd`, `--output-format json`. codex-cli 0.153.4: `exec`, `--sandbox`,
    `-a never`, `--json`, `-C`, `-o`."""
    if executor == "claude_cli":
        return [
            "claude",
            "-p",
            prompt,
            "--permission-mode",
            "default",
            "--allowedTools",
            "Read,Edit,Write,Grep,Glob,Bash(git *),Bash(python -m pytest *),Bash(ruff *)",
            "--max-budget-usd",
            "0",
            "--output-format",
            "json",
        ]
    if executor == "codex_cli":
        return [
            "codex",
            "exec",
            "--sandbox",
            "workspace-write",
            "-a",
            "never",
            "--json",
            "-C",
            str(worktree),
            "-o",
            str(output_path),
            prompt,
        ]
    raise ValueError("scripted executor has no agent argv")


def _git(repo, *args, timeout=60):
    done = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {args[0]}: {done.stderr.strip()[:300]}")
    return done.stdout.strip()


def scrubbed_env(home, worktree):
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "PYTHONHASHSEED": "0",
        "TZ": "UTC",
        "PYTHONPATH": str(Path(worktree) / "src"),
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    if "LANG" in os.environ:
        env["LANG"] = os.environ["LANG"]
    return env


class CodingService:
    def __init__(self, cfg, center, *, repo=None, python=None):
        self.cfg = cfg
        self.center = center
        self.repo = Path(repo or cfg.repo or Path(__file__).resolve().parents[3])
        self.python = python or sys.executable
        self.workspace = cfg.root / "coding"
        self.processes = {}  # run_id -> live process record

    # Capacity -----------------------------------------------------------------------------

    def live_processes(self):
        return sum(1 for p in self.processes.values() if p["popen"].poll() is None)

    # Commands -----------------------------------------------------------------------------

    def start(self, payload, now=None):
        brief, patch = validate_brief(payload)
        active = self.center.runs(kind="maintenance", states=ACTIVE_STATES)
        if len(active) >= MAX_WRITERS:
            raise ValueError(f"max_writers: {len(active)} maintenance runs already active")
        if any(r["pins"].get("brief_sha256") == brief["brief_sha256"] for r in active):
            raise ValueError("run_active: the same frozen brief is already running")
        base = _git(self.repo, "rev-parse", "HEAD")
        run_id = new_id()
        run_dir = self.workspace / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        (run_dir / "brief.json").write_text(
            json.dumps(brief, indent=1, sort_keys=True), encoding="utf-8"
        )
        if patch is not None:
            (run_dir / "patch.diff").write_text(patch, encoding="utf-8")
        pins = {
            "issue": brief["issue"],
            "brief_sha256": brief["brief_sha256"],
            "base_commit": base,
            "executor": brief["executor"],
            "simulated": brief["simulated"],
            "patch_sha256": brief["patch_sha256"],
            "repo_path_sha256": hashlib.sha256(str(self.repo).encode()).hexdigest(),
            "code_hash": source_hash(),
            "writer_id": "writer-1",
            "reviewer_id": "reviewer-1",
        }
        self.center.create_run(
            run_id,
            "maintenance",
            pins,
            "maintenance_v1",
            brief["limits"],
            "coding-" + run_id[:8],
            now,
        )
        self.center.set_run(
            run_id,
            state="running",
            detail={
                "phase": "writer",
                "integration": "not merged (manual review by Codex)",
                "started_at": time.time(),
            },
            now=now,
        )
        return run_id

    def pause(self, run, now=None):
        self.center.set_run(
            run["run_id"],
            paused=True,
            state="paused" if run["state"] == "running" else None,
            now=now,
        )

    def resume(self, run, now=None):
        self.center.set_run(
            run["run_id"],
            paused=False,
            state="running" if run["state"] == "paused" else None,
            now=now,
        )

    def cancel(self, run, now=None):
        self._kill(run["run_id"])
        detail = dict(run["detail"], phase="canceled")
        self.center.set_run(
            run["run_id"], state="canceled", outcome="canceled", detail=detail, now=now
        )

    # Loop ---------------------------------------------------------------------------------

    def step(self, now=None):
        changed = []
        for run in self.center.runs(kind="maintenance", states=("running", "paused")):
            detail = dict(run["detail"])
            if run["state"] == "paused" and run["run_id"] not in self.processes:
                continue  # pause: no new phase; a running check finishes
            try:
                if (
                    time.time() - detail.get("started_at", time.time())
                    > run["limits"]["wall_seconds"]
                ):
                    self._kill(run["run_id"])
                    self._finish(run, "failed", "budget_exhausted", detail, now)
                    changed.append((run["run_id"], "budget_exhausted"))
                    continue
                phase = detail.get("phase")
                if phase == "writer":
                    self._writer(run, detail, now)
                elif phase in ("checks", "review_checks"):
                    self._poll_checks(run, detail, now)
                elif phase == "review":
                    self._review(run, detail, now)
                self.center.set_run(run["run_id"], detail=detail, now=now)
                if detail.get("phase") == "done":
                    self._finish(run, "completed", detail["recommendation"], detail, now)
                    changed.append((run["run_id"], detail["recommendation"]))
            except (RuntimeError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
                detail["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
                self._kill(run["run_id"])
                self._finish(run, "failed", detail.get("outcome") or "phase_failed", detail, now)
                changed.append((run["run_id"], "failed"))
        return changed

    def _finish(self, run, state, outcome, detail, now):
        detail.pop("started_at", None)
        detail["finished_phase"] = detail.get("phase")
        self.center.set_run(run["run_id"], state=state, outcome=outcome, detail=detail, now=now)

    def _kill(self, run_id):
        record = self.processes.pop(run_id, None)
        if record and record["popen"].poll() is None:
            try:
                os.killpg(record["popen"].pid, signal.SIGTERM)
                record["popen"].wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(record["popen"].pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    # Phases -------------------------------------------------------------------------------

    def _run_dir(self, run):
        return self.workspace / run["run_id"]

    def _writer(self, run, detail, now):
        run_dir = self._run_dir(run)
        worktree = run_dir / run["pins"]["writer_id"]
        branch = f"cc/{run['run_id'][:8]}/{run['pins']['writer_id']}"
        if not worktree.exists():
            _git(
                self.repo,
                "worktree",
                "add",
                "-b",
                branch,
                str(worktree),
                run["pins"]["base_commit"],
            )
        detail["writer_worktree"] = str(worktree)
        detail["branch"] = branch
        if run["pins"]["executor"] == "scripted":
            _git(worktree, "apply", "--index", str(run_dir / "patch.diff"))
            env = {
                **scrubbed_env(run_dir, worktree),
                "GIT_AUTHOR_NAME": "cc-writer-1",
                "GIT_AUTHOR_EMAIL": "writer-1@command-center.invalid",
                "GIT_COMMITTER_NAME": "cc-writer-1",
                "GIT_COMMITTER_EMAIL": "writer-1@command-center.invalid",
            }
            done = subprocess.run(
                [
                    "git",
                    "-C",
                    str(worktree),
                    "commit",
                    "-q",
                    "-m",
                    f"cc({run['run_id'][:8]}): {run['pins']['issue']}\n\nSimulated writer: operator-supplied patch applied by the Command Center pipeline.",
                ],
                capture_output=True,
                text=True,
                timeout=60,
                env=env,
            )
            if done.returncode != 0:
                raise RuntimeError("commit failed: " + done.stderr.strip()[:200])
            detail["commit"] = _git(worktree, "rev-parse", "HEAD")
            detail["writer"] = {
                "executor": "scripted",
                "simulated": True,
                "note": "operator-supplied patch; not an agent",
            }
        else:
            prompt = (
                json.loads((run_dir / "brief.json").read_text())["prompt"]
                or f"{run['pins']['issue']}"
            )
            argv = agent_argv(
                run["pins"]["executor"], worktree, prompt, run_dir / "agent-last-message.json"
            )
            detail["executor_argv"] = argv
            record = authorization(self.cfg.root)
            if record is None or run["pins"]["executor"] not in record.get("executors", []):
                detail["blocker"] = (
                    f"execution_disabled: no {AUTHORIZATION_FILE} in the root names established $0-incremental usage for "
                    f"{run['pins']['executor']}; a signed-in CLI is not proof of a billing allowance"
                )
                detail["outcome"] = "execution_disabled"
                raise ValueError(detail["blocker"])
            raise ValueError(
                "real agent execution is not exercised in this pilot even when authorized"
            )
        changed = _git(
            worktree, "diff", "--name-only", f"{run['pins']['base_commit']}..{detail['commit']}"
        ).splitlines()
        deleted = _git(
            worktree,
            "diff",
            "--diff-filter=D",
            "--name-only",
            f"{run['pins']['base_commit']}..{detail['commit']}",
        ).splitlines()
        brief = json.loads((run_dir / "brief.json").read_text())
        findings = self._policy(changed, deleted, brief)
        detail["files_changed"] = changed
        detail["policy_findings"] = findings
        if findings:
            detail["outcome"] = "policy_violation"
            detail["recommendation"] = "reject"
            detail["phase"] = "done"
            detail["review"] = {
                "verdict": "reject",
                "reason": "policy",
                "reviewer": run["pins"]["reviewer_id"],
            }
            return
        detail["phase"] = "checks"
        detail["checks"] = []
        detail["check_queue"] = list(brief["checks"])

    @staticmethod
    def _policy(changed, deleted, brief):
        findings = []
        for path in changed:
            for prefix in brief["protected_paths"]:
                if path == prefix or path.startswith(prefix):
                    findings.append({"kind": "protected_path", "path": path})
        for path in deleted:
            if path.startswith("tests/"):
                findings.append({"kind": "deleted_test", "path": path})
        if not changed:
            findings.append({"kind": "empty_patch"})
        return findings

    def _spawn_check(self, run, detail, worktree, check, phase_key):
        run_dir = self._run_dir(run)
        argv = [self.python, *CHECKS[check["name"]](check["paths"])]
        log = run_dir / f"{phase_key}-{len(detail[phase_key])}-{check['name']}.log"
        handle = open(log, "wb")
        popen = subprocess.Popen(
            argv,
            cwd=str(worktree),
            env=scrubbed_env(run_dir, worktree),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self.processes[run["run_id"]] = {
            "popen": popen,
            "handle": handle,
            "log": log,
            "check": check,
            "phase_key": phase_key,
            "argv": argv,
            "started": time.time(),
        }

    def _poll_checks(self, run, detail, now):
        phase_key = "checks" if detail["phase"] == "checks" else "reviewer_checks"
        worktree = Path(
            detail["writer_worktree"] if phase_key == "checks" else detail["reviewer_worktree"]
        )
        record = self.processes.get(run["run_id"])
        if record is not None:
            code = record["popen"].poll()
            if code is None:
                return
            record["handle"].close()
            self.processes.pop(run["run_id"], None)
            tail = record["log"].read_bytes()[-LOG_TAIL:].decode("utf-8", errors="replace")
            detail[phase_key].append(
                {
                    "name": record["check"]["name"],
                    "argv": record["argv"],
                    "exit_code": code,
                    "started_at": record["started"],
                    "ended_at": time.time(),
                    "log": str(record["log"]),
                    "tail": tail,
                    "commit": detail["commit"],
                }
            )
        queue = detail.get("check_queue") or []
        if queue:
            check = queue.pop(0)
            detail["check_queue"] = queue
            self._spawn_check(run, detail, worktree, check, phase_key)
            return
        if phase_key == "checks":
            detail["phase"] = "review"
        else:
            self._packet(run, detail, now)

    def _review(self, run, detail, now):
        run_dir = self._run_dir(run)
        worktree = run_dir / run["pins"]["reviewer_id"]
        if not worktree.exists():
            _git(self.repo, "worktree", "add", "--detach", str(worktree), detail["commit"])
        if _git(worktree, "rev-parse", "HEAD") != detail["commit"]:
            raise RuntimeError("reviewer worktree is not at the writer's commit")
        detail["reviewer_worktree"] = str(worktree)
        brief = json.loads((run_dir / "brief.json").read_text())
        detail["reviewer_checks"] = []
        detail["check_queue"] = list(brief["checks"])
        detail["phase"] = "review_checks"

    def _packet(self, run, detail, now):
        run_dir = self._run_dir(run)
        brief = json.loads((run_dir / "brief.json").read_text())
        writer_codes = [(c["name"], c["exit_code"]) for c in detail["checks"]]
        reviewer_codes = [(c["name"], c["exit_code"]) for c in detail["reviewer_checks"]]
        findings = list(detail.get("policy_findings") or [])
        if writer_codes != reviewer_codes:
            findings.append(
                {"kind": "check_disagreement", "writer": writer_codes, "reviewer": reviewer_codes}
            )
        if any(code != 0 for _, code in reviewer_codes):
            findings.append({"kind": "checks_failed", "reviewer": reviewer_codes})
        verdict = "accept" if not findings else "reject"
        packet = {
            "kind": "cc_review_packet",
            "version": 1,
            "run_id": run["run_id"],
            "issue": brief["issue"],
            "acceptance_criteria": brief["acceptance_criteria"],
            "brief_sha256": brief["brief_sha256"],
            "base_commit": run["pins"]["base_commit"],
            "commit": detail["commit"],
            "branch": detail["branch"],
            "writer": {
                "id": run["pins"]["writer_id"],
                "executor": run["pins"]["executor"],
                "simulated": run["pins"]["simulated"],
            },
            "reviewer": {
                "id": run["pins"]["reviewer_id"],
                "kind": "deterministic policy + independent re-run of the trusted checks",
            },
            "files_changed": detail["files_changed"],
            "writer_checks": [
                {k: c[k] for k in ("name", "argv", "exit_code", "commit")} for c in detail["checks"]
            ],
            "reviewer_checks": [
                {k: c[k] for k in ("name", "argv", "exit_code", "commit")}
                for c in detail["reviewer_checks"]
            ],
            "findings": findings,
            "verdict": verdict,
            "recommendation": verdict,
            "integration": "not merged: a human/Codex decides; this packet is advisory",
            "note": "worker claims are unverified until the trusted checks and this review establish them; acceptance criteria are judged by the human reader against the diff",
        }
        data = json.dumps(packet, indent=1, sort_keys=True).encode()
        (run_dir / "review-packet.json").write_bytes(data)
        lines = [
            f"# Review packet {run['run_id'][:8]}: {brief['issue']}",
            "",
            f"commit `{detail['commit']}` on `{detail['branch']}` (base `{run['pins']['base_commit']}`), writer {run['pins']['writer_id']} ({run['pins']['executor']}{', simulated' if run['pins']['simulated'] else ''}), reviewer {run['pins']['reviewer_id']}",
            "",
            "## Acceptance criteria",
            *[f"- {a}" for a in brief["acceptance_criteria"]],
            "",
            "## Files",
            *[f"- `{f}`" for f in detail["files_changed"]],
            "",
            "## Checks (writer worktree / reviewer worktree, exit codes)",
            *[
                f"- {w['name']}: {w['exit_code']} / {r['exit_code']}"
                for w, r in zip(detail["checks"], detail["reviewer_checks"])
            ],
            "",
            f"## Verdict: **{verdict}**",
            *[f"- {json.dumps(f)}" for f in findings],
            "",
            packet["integration"],
        ]
        (run_dir / "review-packet.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        detail["review"] = {
            "verdict": verdict,
            "findings": findings,
            "reviewer": run["pins"]["reviewer_id"],
            "packet_sha256": hashlib.sha256(data).hexdigest(),
            "packet": str(run_dir / "review-packet.json"),
        }
        detail["recommendation"] = verdict
        detail["phase"] = "done"
        detail.pop("check_queue", None)

    def idle(self):
        return not self.center.runs(kind="maintenance", states=ACTIVE_STATES)

    def remove_worktrees(self, run_id):
        """Operator cleanup after a run is archived; evidence directories stay."""
        for name in ("writer-1", "reviewer-1"):
            path = self.workspace / run_id / name
            if path.exists():
                try:
                    _git(self.repo, "worktree", "remove", "--force", str(path))
                except RuntimeError:
                    shutil.rmtree(path, ignore_errors=True)


def terminal(state):
    return state in TERMINAL_RUN_STATES
