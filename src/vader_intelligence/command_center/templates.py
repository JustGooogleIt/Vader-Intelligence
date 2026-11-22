"""Approved farm task templates and roles. A role is available only when it maps to an
approved declarative operation that exists today; anything that would need an LLM or a
model registry is declared unavailable rather than pretended."""

from ..pods.game_ops import BASELINE_KIND, CHECK_KIND, SUMMARY_KIND

ROLES = {
    "evidence_inspector": {
        "available": True,
        "operation": "cc_evidence_summary",
        "stage": 1,
        "emits": SUMMARY_KIND,
        "description": "summarises the staged evidence snapshot (fixed Python operation)",
    },
    "baseline_analyst": {
        "available": True,
        "operation": "cc_baseline_analysis",
        "stage": 1,
        "emits": BASELINE_KIND,
        "description": "market midpoint and constant baselines from the staged books (fixed Python operation; not a forecast)",
    },
    "independent_checker": {
        "available": True,
        "operation": "cc_independent_check",
        "stage": 2,
        "emits": CHECK_KIND,
        "requires": ("evidence_inspector", "baseline_analyst"),
        "description": "recomputes stage-1 claims from the same evidence and reports disagreements",
    },
    "model_analyst": {
        "available": False,
        "reason": "needs a registry model input; not wired in v1 (no registry path is passed to workers)",
    },
    "llm_agent": {
        "available": False,
        "reason": "no authorized provider usage; paid calls are disabled and no agent adapter runs in farms",
    },
}

TEMPLATES = {
    "evidence_review_v1": {
        "description": "evidence inspection, baseline analysis and an independent check — fixed Python operations under the pods boundary, not an LLM agent farm",
        "roles": ("evidence_inspector", "baseline_analyst", "independent_checker"),
    }
}

LIMITS = {"max_concurrent": (1, 2), "max_tasks": (1, 8), "wall_seconds": (30, 3600)}
EXECUTION_MODES = ("pods",)


def validate_request(payload):
    """Normalized start request or ValueError (the reason becomes the command's receipt)."""
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    template = payload.get("template")
    if template not in TEMPLATES:
        raise ValueError(f"unknown template {template!r}")
    roles = tuple(payload.get("roles") or TEMPLATES[template]["roles"])
    if not roles or len(set(roles)) != len(roles):
        raise ValueError("roles must be a non-empty list without duplicates")
    for role in roles:
        spec = ROLES.get(role)
        if spec is None:
            raise ValueError(f"unknown role {role!r}")
        if not spec["available"]:
            raise ValueError(f"role {role!r} unavailable: {spec['reason']}")
        if role not in TEMPLATES[template]["roles"]:
            raise ValueError(f"role {role!r} is not part of {template}")
    for role in roles:
        for needed in ROLES[role].get("requires", ()):
            if needed not in roles:
                raise ValueError(f"role {role!r} requires {needed!r}")
    limits = dict(payload.get("limits") or {})
    normalized = {}
    for name, (low, high) in LIMITS.items():
        value = limits.get(name, high if name == "wall_seconds" else low)
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise ValueError(f"limit {name} must be an integer in {low}..{high}")
        normalized[name] = value
    if normalized["max_tasks"] < len(roles):
        raise ValueError("max_tasks is smaller than the number of roles")
    execution = payload.get("execution", "pods")
    if execution not in EXECUTION_MODES:
        raise ValueError(f"execution mode {execution!r} is not authorized")
    game_id = payload.get("game_id")
    if not isinstance(game_id, int) or isinstance(game_id, bool) or game_id <= 0:
        raise ValueError("game_id must be a positive integer")
    return {
        "template": template,
        "roles": roles,
        "limits": normalized,
        "execution": execution,
        "game_id": game_id,
    }


def catalogue():
    return {
        "templates": {k: dict(v, roles=list(v["roles"])) for k, v in TEMPLATES.items()},
        "roles": {
            k: {kk: (list(vv) if isinstance(vv, tuple) else vv) for kk, vv in v.items()}
            for k, v in ROLES.items()
        },
        "limits": {k: list(v) for k, v in LIMITS.items()},
        "execution_modes": list(EXECUTION_MODES),
    }
