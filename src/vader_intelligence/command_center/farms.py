"""Game-research farm runs: evidence snapshots, run pins, durable commands and stage
dispatch through the pods coordinator (J3).

The service never spawns a process: it stages an evidence snapshot as a coordinator
input, registers a pod per run, submits approved operations stage by stage, and asks the
coordinator to cancel owned tasks. The coordinator (held by the controller) owns leases,
process groups, retries and quarantine."""

import hashlib
import json
import logging
import time

from ..pods.contracts import Budgets, ExperimentManifest, PodSpec, TaskSpec
from ..pods.game_ops import BASELINE_KIND, CHECK_KIND, EVIDENCE_KIND, EVIDENCE_VERSION, SUMMARY_KIND
from ..provenance import source_hash
from . import projections
from .config import CenterConfig
from .store import TERMINAL_RUN_STATES, CenterStore, new_id
from .templates import ROLES, validate_request

log = logging.getLogger(__name__)
TERMINAL_TASK_STATES = ("succeeded", "failed", "partial", "canceled", "quarantined")
SUCCESS_TASK_STATES = ("succeeded", "partial")
ACTIVE_RUN_STATES = ("created", "running", "paused", "canceling")


def build_evidence(cfg: CenterConfig, game_id, now=None):
    """The evidence snapshot a run's workers see, built read-only from the archive, and
    the run pins. Refuses an ambiguous or unknown game (ValueError with the reason)."""
    view = projections.game(cfg, game_id, now)
    if view["state"] != "ok":
        raise ValueError(f"game unavailable: {view.get('reason') or view['state']}")
    head = view["game"]
    if not head["can_launch"]:
        raise ValueError("identity ambiguous: " + ", ".join(head["identity_reasons"]))
    evidence = {
        "kind": EVIDENCE_KIND,
        "version": EVIDENCE_VERSION,
        "game_id": game_id,
        "event_ticker": head["event_ticker"],
        "market_tickers": view["pins"]["market_tickers"],
        "scheduled_start": head["scheduled_start"],
        "cutoff": head["cutoff"],
        "label": cfg.labels.get("archive"),
        "source_time": view["source_time"],
        "eligibility": [
            {
                k: e.get(k)
                for k in (
                    "id",
                    "run_id",
                    "ticker",
                    "checked_at",
                    "game_id",
                    "eligible",
                    "reason",
                    "cutoff",
                    "schedule_fetch_id",
                    "market_fetch_id",
                )
            }
            for e in view["eligibility"]
        ],
        "observations": [
            {
                k: o.get(k)
                for k in (
                    "id",
                    "fetch_id",
                    "ticker",
                    "kind",
                    "parser_version",
                    "retrieved_at",
                    "data",
                )
            }
            for o in view["observations"]
        ],
        "fetches": view["fetches"],
    }
    data = json.dumps(evidence, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    pins = {
        "game_id": game_id,
        "event_ticker": head["event_ticker"],
        "market_tickers": view["pins"]["market_tickers"],
        "cutoff": head["cutoff"],
        "scheduled_start": head["scheduled_start"],
        "source_mode": "archive:" + (cfg.labels.get("archive") or "unlabelled"),
        "source_runs": view["pins"]["source_runs"],
        "source_time": view["source_time"],
        "evidence_sha256": hashlib.sha256(data).hexdigest(),
        "code_hash": source_hash(),
        "archive_path_sha256": hashlib.sha256(str(cfg.archive).encode()).hexdigest(),
    }
    return data, pins


class FarmService:
    """Applies durable commands and dispatches stages. One instance per controller."""

    def __init__(
        self, cfg: CenterConfig, center: CenterStore, pods, coordinator, *, agent_processes=None
    ):
        self.cfg = cfg
        self.center = center
        self.pods = pods
        self.coordinator = coordinator
        # () -> number of registered non-pods agent processes (coding farm), for the cap
        self.agent_processes = agent_processes or (lambda: 0)

    # Capacity -----------------------------------------------------------------------------

    def active_processes(self):
        return len(self.pods.open_attempts()) + self.agent_processes()

    def headroom(self):
        """Global cap minus running attempts minus tasks already queued (they will run)."""
        queued = self.pods.db.execute(
            "SELECT COUNT(*) FROM tasks WHERE state IN ('queued','retry_wait','leased')"
        ).fetchone()[0]
        return (
            self.cfg.global_agent_cap
            - self.active_processes()
            - max(0, queued - len(self.pods.open_attempts()))
        )

    # Commands -----------------------------------------------------------------------------

    def apply_commands(self, now=None):
        applied = []
        for command in self.center.pending_commands():
            payload = json.loads(command["payload_json"])
            try:
                if command["kind"] == "start":
                    run_id = self.start(payload, now)
                    self.center.settle_command(
                        command["command_id"], "applied", None, now, {"run_id": run_id}
                    )
                    applied.append((command["command_id"], "applied", run_id))
                    continue
                run = self.center.run(command["run_id"] or "")
                if run is None:
                    raise ValueError("unknown run")
                if run["state"] in TERMINAL_RUN_STATES:
                    raise ValueError(f"run is {run['state']}")
                if command["kind"] == "pause":
                    self.center.set_run(
                        run["run_id"],
                        paused=True,
                        state="paused" if run["state"] == "running" else None,
                        now=now,
                    )
                elif command["kind"] == "resume":
                    self.center.set_run(
                        run["run_id"],
                        paused=False,
                        state="running" if run["state"] == "paused" else None,
                        now=now,
                    )
                elif command["kind"] == "cancel":
                    self.cancel(run, now)
                self.center.settle_command(
                    command["command_id"], "applied", None, now, {"run_id": run["run_id"]}
                )
                applied.append((command["command_id"], "applied", run["run_id"]))
            except ValueError as exc:
                self.center.settle_command(command["command_id"], "rejected", str(exc)[:300], now)
                applied.append((command["command_id"], "rejected", str(exc)[:80]))
        return applied

    def start(self, payload, now=None):
        request = validate_request(payload)
        game_id = request["game_id"]
        for run in self.center.runs(kind="game_research", states=ACTIVE_RUN_STATES):
            if run["pins"].get("game_id") == game_id:
                raise ValueError(f"run_active: {run['run_id']} already runs for game {game_id}")
            raise ValueError(
                f"one_active_farm_limit: {run['run_id']} is active for game {run['pins'].get('game_id')}"
            )
        data, pins = build_evidence(self.cfg, game_id, now)
        pins["evidence_sha256"] = self.pods.put_input(data, EVIDENCE_KIND, now)
        pins["template"] = request["template"]
        pins["roles"] = list(request["roles"])
        pins["execution"] = request["execution"]
        run_id = new_id()
        pod_id = "farm-" + run_id[:8]
        limits = request["limits"]
        self.pods.register_pod(
            PodSpec(
                pod_id,
                "diagnostics",
                Budgets(
                    max_concurrent=limits["max_concurrent"],
                    # The pod budget counts attempts (a lost attempt retried once is two);
                    # the run's own `max_tasks` bounds distinct tasks in `_submit`.
                    tasks_per_day=limits["max_tasks"] * 2,
                    wall_seconds_per_task=limits["wall_seconds"],
                ),
            ),
            now,
        )
        self.center.create_run(
            run_id, "game_research", pins, request["template"], limits, pod_id, now
        )
        self.center.set_run(run_id, state="running", now=now)
        return run_id

    def cancel(self, run, now=None):
        for binding in run["tasks"]:
            task = self.pods.task(binding["task_id"])
            if task and task["state"] not in TERMINAL_TASK_STATES:
                self.pods.request_cancel(binding["task_id"], now)
        self.center.set_run(run["run_id"], state="canceling", paused=True, now=now)

    # Stages -------------------------------------------------------------------------------

    def _manifest(self, run, operation, params, seed=0):
        manifest = ExperimentManifest(
            dataset_manifest_hash=run["pins"]["evidence_sha256"],
            feature_set_versions={EVIDENCE_KIND: f"v{EVIDENCE_VERSION}"},
            split={
                "development": {"game_id": run["pins"]["game_id"], "cutoff": run["pins"]["cutoff"]}
            },
            code_hash=run["pins"]["code_hash"],
            operation=operation,
            params=params,
            seed=seed,
            budget={
                "wall_seconds": run["limits"]["wall_seconds"],
                "max_tasks": run["limits"]["max_tasks"],
            },
        )
        return self.pods.put_experiment_manifest(manifest)

    def _submit(self, run, role, stage, params, now=None):
        if len(run["tasks"]) >= run["limits"]["max_tasks"]:
            raise ValueError("max_tasks reached")
        operation = ROLES[role]["operation"]
        manifest = self._manifest(run, operation, params)
        task_id = self.pods.submit(TaskSpec(run["pod_id"], operation, params, manifest, 0, 2), now)
        self.center.bind_task(run["run_id"], task_id, role, stage)
        return task_id

    def _task_states(self, run):
        return {
            b["task_id"]: (self.pods.task(b["task_id"]) or {}).get("state", "unknown")
            for b in run["tasks"]
        }

    def _artifact(self, task_id, kind):
        result = self.pods.result(task_id)
        if result is None:
            return None
        for ref in result["result"].get("artifacts", []):
            if ref.get("kind") == kind:
                return ref["sha256"]
        return None

    def reconcile(self, now=None):
        """Progress run states from task states; finalize runs whose work is done."""
        changed = []
        for run in self.center.runs(kind="game_research", states=ACTIVE_RUN_STATES):
            states = self._task_states(run)
            if run["state"] == "canceling":
                if all(s in TERMINAL_TASK_STATES for s in states.values()):
                    self.center.set_run(
                        run["run_id"], state="canceled", outcome="canceled", now=now
                    )
                    changed.append((run["run_id"], "canceled"))
                continue
            if (
                run["stage"] >= 1
                and states
                and any(s in ("failed", "quarantined", "canceled") for s in states.values())
            ):
                bad = {
                    t: s for t, s in states.items() if s in ("failed", "quarantined", "canceled")
                }
                self.center.set_run(
                    run["run_id"],
                    state="failed",
                    outcome="task_failed",
                    detail={"tasks": bad},
                    now=now,
                )
                changed.append((run["run_id"], "failed"))
                continue
            if run["stage"] == 2 and all(s in TERMINAL_TASK_STATES for s in states.values()):
                checker = [b for b in run["tasks"] if b["role"] == "independent_checker"]
                verdict = None
                if checker:
                    result = self.pods.result(checker[0]["task_id"])
                    output = result["result"].get("output") if result else None
                    verdict = (output or {}).get("verdict")
                self.center.set_run(
                    run["run_id"],
                    state="completed",
                    outcome=verdict or "completed",
                    detail={"check_verdict": verdict},
                    now=now,
                )
                changed.append((run["run_id"], "completed"))
        return changed

    def dispatch(self, now=None):
        """Submit the next stage of each running, unpaused run within caps."""
        submitted = []
        for run in self.center.runs(kind="game_research", states=("running",)):
            if run["paused"]:
                continue
            states = self._task_states(run)
            roles = run["pins"]["roles"]
            if run["stage"] == 0:
                first = [r for r in roles if ROLES[r]["stage"] == 1]
                if self.headroom() < len(first):
                    log.info("run %s waits for capacity (%s needed)", run["run_id"], len(first))
                    continue
                params = {
                    "evidence_sha256": run["pins"]["evidence_sha256"],
                    "game_id": run["pins"]["game_id"],
                }
                for role in first:
                    submitted.append(self._submit(run, role, 1, params, now))
                self.center.set_run(run["run_id"], stage=1, now=now)
            elif (
                run["stage"] == 1
                and states
                and all(s in TERMINAL_TASK_STATES for s in states.values())
            ):
                second = [r for r in roles if ROLES[r]["stage"] == 2]
                if not second:
                    self.center.set_run(
                        run["run_id"], state="completed", outcome="completed", now=now
                    )
                    continue
                if self.headroom() < len(second):
                    continue
                by_role = {b["role"]: b["task_id"] for b in run["tasks"]}
                summary = self._artifact(by_role.get("evidence_inspector", ""), SUMMARY_KIND)
                baseline = self._artifact(by_role.get("baseline_analyst", ""), BASELINE_KIND)
                if not summary or not baseline:
                    self.center.set_run(
                        run["run_id"], state="failed", outcome="stage1_artifacts_missing", now=now
                    )
                    continue
                params = {
                    "evidence_sha256": run["pins"]["evidence_sha256"],
                    "game_id": run["pins"]["game_id"],
                    "summary_sha256": summary,
                    "baseline_sha256": baseline,
                }
                for role in second:
                    submitted.append(self._submit(run, role, 2, params, now))
                self.center.set_run(run["run_id"], stage=2, now=now)
        return submitted

    def check_kind(self):
        return CHECK_KIND

    def idle(self):
        return not self.center.pending_commands() and not self.center.runs(
            kind="game_research", states=ACTIVE_RUN_STATES
        )


def elapsed(started):
    return time.time() - started
