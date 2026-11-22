"""Bounded research planning for pods (roadmap G): which approved operation runs next.

A planner reads a development-only context and proposes `(operation, params)` pairs.
Nothing a planner returns is executed as code: every proposal must name an operation the
pod's role may run and must pass that operation's typed parameter contract, exactly like
an operator submission; anything else is recorded as rejected and never submitted.

The context carries the pod's own finished tasks on one snapshot with an allowlist of
development-only fields (cross-validation or inner-split scores, selected configuration,
search counters). Validation diagnostics, the holdout and candidate state never enter it,
so planning cannot adapt to the validation rows; the protected evaluator remains the only
judge of a proposal (`pods.evaluator`), and every accepted proposal is counted there.

`HeuristicPlanner` is deterministic and makes no model call. `ProviderPlanner` asks a
`ModelProvider` for JSON proposals; paid calls are disabled in this release, so it runs
only against the scripted double, and a real vendor raises `PaidCallsDisabled`.
"""

import hashlib
import json
import math
from dataclasses import dataclass, field

from ..models.search import FAMILIES as SEARCH_FAMILIES
from ..provenance import json_text
from .contracts import ContractError, content_hash, finite_number
from .operations import (
    TRAIN_HYPERPARAMETERS,
    UnknownOperation,
    get_operation,
    normalized_params,
)
from .providers import ModelProvider, PaidCallsDisabled, ProviderRequest
from .snapshot import feature_set_version, load

PLANNED_OPERATIONS = {
    "baseline_models": ("train_model",),
    "symbolic_search": ("symbolic_search",),
}
SCORE_FIELDS = {
    "train_model": "development_cv_log_loss",
    "symbolic_search": "development_inner_log_loss",
}
# Result fields a planner may read, per operation: development-only by construction.
VISIBLE_FIELDS = {
    "train_model": (
        "family",
        "selected_config",
        "configs_tried",
        "search_stop_reason",
        "grid",
        "development_rows",
        "development_cv_log_loss",
    ),
    "symbolic_search": (
        "status",
        "stop_reason",
        "candidates_evaluated",
        "generations_completed",
        "expression",
        "complexity",
        "development_inner_log_loss",
    ),
}
POD_FAMILIES = {"logistic": "logistic", "gradient_boosting": "gbm"}
MAX_RESPONSE_BYTES = 65536
MAX_RATIONALE = 500
MAX_REASON = 300


def _refuse_constant(name):
    raise ValueError(f"non-finite JSON constant {name}")


@dataclass(frozen=True)
class Proposal:
    operation: str
    params: dict
    rationale: str = ""


@dataclass(frozen=True)
class Plan:
    proposals: tuple
    rejected: tuple = ()  # (rejection(item, reason), ...): hashes and reasons only
    provider: dict = field(default_factory=dict)  # request/response hashes, never text
    stop: str | None = None  # why the planner proposes nothing further


def score(result, operation):
    value = result.get(SCORE_FIELDS.get(operation, ""))
    return value if finite_number(value) else None


def context(store, pod_id, snapshot_sha, *, tasks_remaining, rounds_remaining):
    """Development-only planning context for `pod_id` on one snapshot."""
    pod = store.db.execute("SELECT role FROM pods WHERE pod_id=?", (pod_id,)).fetchone()
    if pod is None:
        raise ContractError(f"unknown pod {pod_id!r}")
    snapshot = load(store.artifacts.get_bytes(snapshot_sha))
    history = []
    rows = store.db.execute(
        "SELECT task_id, type, params_json, state FROM tasks WHERE pod_id=? ORDER BY created_at,"
        " task_id",
        (pod_id,),
    )
    for task_id, operation, params_json, state in rows:
        params = json.loads(params_json)
        if params.get("dataset_manifest_hash") != snapshot_sha:
            continue
        entry = {"task_id": task_id, "operation": operation, "params": params, "state": state}
        result = store.result(task_id)
        if result is not None:
            output = result["result"].get("output") or {}
            entry["result"] = {
                k: output[k] for k in VISIBLE_FIELDS.get(operation, ()) if k in output
            }
        history.append(entry)
    return {
        "pod": pod_id,
        "role": pod[0],
        "snapshot": {
            "sha256": snapshot_sha,
            "feature_set": snapshot.feature_set,
            "feature_set_version": feature_set_version(snapshot.manifest),
            "features": [f["name"] for f in snapshot.manifest["feature_set"]["features"]],
            "development_rows": len(snapshot.development),
        },
        "operations": list(PLANNED_OPERATIONS.get(pod[0], ())),
        "history": history,
        "budget": {"tasks_remaining": tasks_remaining, "rounds_remaining": rounds_remaining},
    }


def _fixed_fields(ctx, operation):
    snap = ctx["snapshot"]
    fixed = {"dataset_manifest_hash": snap["sha256"]}
    if operation == "train_model":
        fixed |= {
            "feature_set": snap["feature_set"],
            "feature_set_version": snap["feature_set_version"],
        }
    return fixed


def _check_grid(params):
    """Family grid keys and values; `params` already satisfy the operation contract."""
    family = POD_FAMILIES.get(params.get("model_family"))
    grid = (params.get("hyperparameters") or {}).get("grid")
    if family is None or not grid:
        return
    spec = SEARCH_FAMILIES[family]
    unknown = sorted(set(grid) - spec.required - spec.optional)
    missing = sorted(spec.required - set(grid))
    if unknown or missing:
        raise ContractError(f"grid keys: unknown {unknown}, missing {missing}")
    for name, values in grid.items():
        numbers = all(finite_number(v) and v > 0 for v in values)
        if not numbers:
            raise ContractError(f"grid {name}: values must be positive finite numbers")
        if len(values) > 8:
            raise ContractError(f"grid {name}: at most 8 values")


def validate(ctx, proposal):
    """Typed, contract-checked parameters for an approved operation, or ContractError."""
    if proposal.operation not in ctx["operations"]:
        raise ContractError(f"operation {proposal.operation!r} is not planned for this pod")
    try:
        get_operation(proposal.operation)
    except UnknownOperation as exc:
        raise ContractError(str(exc)) from None
    if not isinstance(proposal.params, dict):
        raise ContractError("params must be an object")
    fixed = _fixed_fields(ctx, proposal.operation)
    for name, value in fixed.items():
        if name in proposal.params and proposal.params[name] != value:
            raise ContractError(f"{name} is fixed by the snapshot")
    params = {**proposal.params, **fixed}
    try:  # untrusted values: every failure is a recorded rejection, never a crash
        normalized = normalized_params(proposal.operation, params)
        _check_grid(normalized)
    except ContractError:
        raise
    except (TypeError, ValueError, AttributeError, KeyError, ArithmeticError) as exc:
        raise ContractError(f"invalid params: {type(exc).__name__}") from None
    return normalized


def rejection(item, reason):
    """A rejected proposal as recorded: its hash, the operation name and the reason."""
    operation = item.get("operation") if isinstance(item, dict) else None
    return {
        "proposal_sha256": hashlib.sha256(
            json.dumps(item, sort_keys=True, default=repr).encode()
        ).hexdigest(),
        "operation": operation[:64] if isinstance(operation, str) else None,
        "reason": reason[:MAX_REASON],
    }


class HeuristicPlanner:
    """Deterministic refinement on development scores; never a model call.

    baseline_models: default grids for logistic and gradient boosting, then refine each
    family around its selected configuration until the development CV log loss stops
    improving by `min_improvement`. symbolic_search: the default search, then larger
    searches while the inner-split log loss improves.
    """

    name = "heuristic-v1"

    def __init__(
        self,
        *,
        seed=0,
        min_improvement=0.0005,
        search_seconds=300.0,
        families=("logistic", "gradient_boosting"),
        grids=None,
        n_folds=None,
        symbolic=None,
    ):
        self.seed = seed
        self.min_improvement = min_improvement
        self.search_seconds = search_seconds
        self.families = tuple(families)
        self.grids = dict(grids or {})  # first grid per family (default: the family's own)
        self.n_folds = n_folds
        self.symbolic = dict(symbolic or {})  # first symbolic search parameters

    def plan(self, ctx):
        if ctx["role"] == "baseline_models":
            proposals = self._baseline(ctx)
        elif ctx["role"] == "symbolic_search":
            proposals = self._symbolic(ctx)
        else:
            return Plan((), stop=f"role {ctx['role']} is not planned")
        return Plan(tuple(proposals), stop=None if proposals else "no_improving_refinement")

    def _done(self, ctx, operation):
        return [h for h in ctx["history"] if h["operation"] == operation]

    def _improving(self, scored):
        """True while the newest score beats the best earlier score by the threshold."""
        if len(scored) < 2:
            return True
        best_before = min(scored[:-1])
        return scored[-1] < best_before - self.min_improvement

    def _baseline(self, ctx):
        out = []
        history = self._done(ctx, "train_model")
        for pod_family in self.families:
            mine = [h for h in history if h["params"]["model_family"] == pod_family]
            hyper = {"search_seconds": self.search_seconds}
            if self.n_folds is not None:
                hyper["n_folds"] = self.n_folds
            if not mine:
                first = self.grids.get(pod_family)
                out.append(
                    self._train(
                        pod_family, hyper | ({"grid": first} if first else {}), "first grid"
                    )
                )
                continue
            if any(h["state"] not in ("succeeded", "partial") for h in mine):
                continue  # unfinished or failed: wait, never repeat a failed spec
            scored = [score(h.get("result", {}), "train_model") for h in mine]
            if None in scored or not self._improving(scored):
                continue
            best = min(mine, key=lambda h: score(h["result"], "train_model"))
            tried = {}  # every value this family has tried, per grid key
            for h in mine:
                for key, values in (h["result"].get("grid") or {}).items():
                    tried.setdefault(key, set()).update(float(v) for v in values)
            grid = self._refine(pod_family, best["result"], tried)
            if grid is not None:
                out.append(self._train(pod_family, hyper | {"grid": grid}, "refine around best"))
        return out

    def _train(self, pod_family, hyper, why):
        return Proposal(
            "train_model",
            {"model_family": pod_family, "seed": self.seed, "hyperparameters": hyper},
            why,
        )

    @staticmethod
    def _refine(pod_family, result, tried):
        config = result["selected_config"]
        if pod_family == "logistic":
            c = float(config["C"])
            values = sorted(tried.get("C", set()) | {c})
            i = values.index(c)
            if i == 0:  # the best is the smallest tried: extend downwards
                grid = [c / 30, c / 10, c / 3]
            elif i == len(values) - 1:
                grid = [c * 3, c * 10, c * 30]
            else:  # bracket between the tried neighbours (geometric midpoints)
                grid = [math.sqrt(values[i - 1] * c), c, math.sqrt(c * values[i + 1])]
            grid = sorted({float(f"{v:.6g}") for v in grid if 1e-6 <= v <= 1e6})
            return {"C": grid} if grid else None
        lr = float(config["learning_rate"])
        iters = int(config["max_iter"])
        leaves = int(config["max_leaf_nodes"])
        return {
            "learning_rate": sorted({round(lr / 2, 6), round(lr, 6)}),
            "max_iter": sorted({iters, min(iters * 2, 1000)}),
            "max_leaf_nodes": sorted({max(3, leaves // 2), leaves, min(leaves * 2, 63)}),
            "min_samples_leaf": [int(config["min_samples_leaf"])],
            "l2_regularization": [float(config["l2_regularization"])],
        }

    def _symbolic(self, ctx):
        history = self._done(ctx, "symbolic_search")
        base = {
            "seed": self.seed,
            "max_candidates": 20000,
            "max_generations": 30,
            "max_seconds": self.search_seconds,
            "population": 200,
        } | self.symbolic
        if not history:
            return [Proposal("symbolic_search", base, "default search")]
        if any(h["state"] not in ("succeeded", "partial") for h in history):
            return []
        scored = [score(h.get("result", {}), "symbolic_search") for h in history]
        if None in scored or not self._improving(scored):
            return []
        last = history[-1]["params"]
        bigger = base | {
            "max_candidates": min(int(last["max_candidates"]) * 2, 1_000_000),
            "max_generations": min(int(last["max_generations"]) * 2, 10_000),
            "population": min(int(last["population"]) * 2, 10_000),
        }
        return [Proposal("symbolic_search", bigger, "larger search while improving")]


class ProviderPlanner:
    """Proposals from a `ModelProvider` as strict JSON; validated like any submission.

    The prompt is the JSON context plus the parameter fields of each planned operation.
    The response must be one JSON object `{"proposals": [{"operation", "params",
    "rationale"}]}`; anything else is rejected and recorded by hash. A real vendor raises
    `PaidCallsDisabled`, which ends planning (recorded, not retried).
    """

    name = "provider-v1"

    def __init__(self, provider: ModelProvider, *, model="unspecified", max_tokens=2048, seed=0):
        self.provider = provider
        self.model = model
        self.max_tokens = max_tokens
        self.seed = seed

    def prompt(self, ctx):
        fields = {
            "train_model": {
                "model_family": ["logistic", "gradient_boosting"],
                "seed": "integer >= 0",
                "hyperparameters": sorted(TRAIN_HYPERPARAMETERS),
                "grid keys": {
                    name: sorted(
                        SEARCH_FAMILIES[family].required | SEARCH_FAMILIES[family].optional
                    )
                    for name, family in POD_FAMILIES.items()
                },
            },
            "symbolic_search": [
                "seed",
                "max_candidates",
                "max_generations",
                "max_seconds",
                "population",
            ],
        }
        allowed = {op: fields[op] for op in ctx["operations"] if op in fields}
        return (
            "Propose the next research tasks for this pod as one JSON object "
            '{"proposals": [{"operation": str, "params": object, "rationale": str}]}. '
            "Only these operations and fields are accepted; the snapshot fields are fixed. "
            "Scores are development-only log losses (lower is better).\n"
            f"OPERATIONS {json_text(allowed)}\nCONTEXT {json_text(ctx)}"
        )

    def plan(self, ctx):
        request = ProviderRequest(
            self.model, self.prompt(ctx), max_tokens=self.max_tokens, seed=self.seed
        )
        provider = {"name": self.provider.name, "request_hash": request.request_hash()}
        try:
            response = self.provider.complete(request)
        except PaidCallsDisabled as exc:
            return Plan((), provider=provider | {"error": str(exc)}, stop="paid_calls_disabled")
        except Exception as exc:  # a provider failure ends planning; it is not retried
            error = type(exc).__name__
            return Plan((), provider=provider | {"error": error}, stop="provider_error")
        provider |= {
            "response_hash": content_hash({"text": response.text}),
            "cost_usd": response.cost_usd,
        }
        if response.cost_usd != 0:
            return Plan((), provider=provider, stop="provider_reported_cost")
        if len(response.text.encode()) > MAX_RESPONSE_BYTES:
            return Plan((), provider=provider, stop="response_too_large")
        try:
            payload = json.loads(response.text, parse_constant=_refuse_constant)
        except (ValueError, RecursionError):
            return Plan((), provider=provider, stop="response_not_json")
        items = payload.get("proposals") if isinstance(payload, dict) else None
        if not isinstance(items, list) or set(payload) != {"proposals"}:
            return Plan((), provider=provider, stop="response_shape")
        proposals, rejected = [], []
        for item in items:
            if not isinstance(item, dict) or not set(item) <= {"operation", "params", "rationale"}:
                rejected.append(rejection(item, "proposal shape"))
                continue
            rationale = item.get("rationale", "")
            proposal = Proposal(
                item.get("operation"),
                item.get("params"),
                rationale[:MAX_RATIONALE] if isinstance(rationale, str) else "",
            )
            try:
                validate(ctx, proposal)
            except ContractError as exc:
                rejected.append(rejection(item, str(exc)))
                continue
            proposals.append(proposal)
        stop = None if proposals else "no_valid_proposal"
        return Plan(tuple(proposals), tuple(rejected), provider, stop)
