"""Bounded genetic symbolic search (docs/specs/symbolic-search-v1.md).

`search` receives a training and a validation fold only; it has no holdout argument.
Budgets are hard: `generations` breeding rounds after the initial population,
`max_candidates` evaluated expressions (an expression is evaluated when its training
outputs are computed), and `max_seconds` on an injectable monotonic clock. All randomness
comes from Generator(PCG64(seed)) and every iteration order is fixed, so equal inputs,
config and seed give an identical `json_text(result.to_dict())` unless time binds.
"""

import hashlib
import math
import time
from dataclasses import asdict, dataclass

import numpy as np

from ..provenance import json_text
from .calibration import log_loss_rows, platt_fit, probabilities
from .expressions import (
    CLIP,
    MAX_DEPTH_LIMIT,
    MAX_NODES_LIMIT,
    canonical_key,
    check_names,
    depth,
    describe,
    evaluate,
    features,
    operators,
    simplify,
    size,
)
from .genetic import MUTATIONS, crossover, mutate, random_tree
from .model import Model, model_to_dict

# v2: the simplest member of a semantic class represents it (v1 kept the first scored, so
# a simpler equivalent found later never reached the front); same seed, same v2 result.
ENGINE_VERSION = "symbolic-search-v2"
SELECTION_RULE = "simplest_within_one_standard_error"
REASONS = ("non_finite", "near_constant", "platt_failure", "unstable")
NEAR_CONSTANT = 1e-9
PERTURBATION = 1e-6
SENSITIVITY = 0.5
FINGERPRINT_RESOLUTION = 1e-9
FRONT_TOLERANCE = 1e-9
RETRY_FACTOR = 10


def _integer(name, value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"{name} must be an integer in [{low}, {high}]")


def _real(name, value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a real number")
    if not (math.isfinite(value) and low <= value <= high):
        raise ValueError(f"{name} must be finite and in [{low}, {high}]")
    return float(value)


@dataclass(frozen=True)
class SearchConfig:
    operator_set: str = "ops-v1"
    max_depth: int = 5
    max_nodes: int = 25
    population: int = 200
    generations: int = 30
    max_candidates: int = 20000
    max_seconds: float = 60.0
    tournament: int = 5
    crossover_rate: float = 0.7
    mutation_rate: float = 0.25
    elitism: int = 5
    complexity_penalty: float = 0.002
    constant_range: tuple = (-2.0, 2.0)
    seed: int = 0

    def __post_init__(self):
        operators(self.operator_set)
        _integer("max_depth", self.max_depth, 2, MAX_DEPTH_LIMIT)
        _integer("max_nodes", self.max_nodes, 3, MAX_NODES_LIMIT)
        _integer("population", self.population, 2, 10**6)
        _integer("generations", self.generations, 0, 10**6)
        _integer("max_candidates", self.max_candidates, 1, 10**9)
        _integer("tournament", self.tournament, 1, self.population)
        _integer("elitism", self.elitism, 0, self.population - 1)
        _integer("seed", self.seed, 0, 2**63 - 1)
        reals = {
            "max_seconds": _real("max_seconds", self.max_seconds, 0.0, 1e9),
            "crossover_rate": _real("crossover_rate", self.crossover_rate, 0.0, 1.0),
            "mutation_rate": _real("mutation_rate", self.mutation_rate, 0.0, 1.0),
            "complexity_penalty": _real("complexity_penalty", self.complexity_penalty, 0.0, 1.0),
        }
        if reals["max_seconds"] <= 0:
            raise ValueError("max_seconds must be positive")
        if reals["crossover_rate"] + reals["mutation_rate"] > 1:
            raise ValueError("crossover_rate + mutation_rate must not exceed 1")
        if not isinstance(self.constant_range, (tuple, list)) or len(self.constant_range) != 2:
            raise ValueError("constant_range must be a (low, high) pair")
        low, high = (_real("constant_range", v, -CLIP, CLIP) for v in self.constant_range)
        if not low < high:
            raise ValueError("constant_range must have low < high")
        for name, value in reals.items():
            object.__setattr__(self, name, value)
        object.__setattr__(self, "constant_range", (low, high))

    def to_dict(self):
        data = asdict(self)
        data["constant_range"] = list(self.constant_range)
        return data

    def config_hash(self):
        return hashlib.sha256(json_text(self.to_dict()).encode()).hexdigest()


def _sha(values):
    return hashlib.sha256(np.ascontiguousarray(values, dtype="<f8").tobytes()).hexdigest()


def _digest(X, y):
    digest = hashlib.sha256(json_text(list(X.shape)).encode())
    digest.update(np.ascontiguousarray(X, dtype="<f8").tobytes())
    digest.update(np.ascontiguousarray(y, dtype="<i8").tobytes())
    return digest.hexdigest()


def _labels(name, y, rows):
    y = np.asarray(y)
    if y.ndim != 1 or y.shape[0] != rows or y.dtype.kind not in "iu":
        raise ValueError(f"{name} must be a 1-D integer array with one label per row")
    if not np.isin(y, (0, 1)).all():
        raise ValueError(f"{name} must contain only 0 and 1")
    return y.astype(np.int64)


@dataclass(frozen=True)
class Candidate:
    """A valid scored expression. Probabilities are summarized by SHA-256 of their
    little-endian float64 bytes so a replay can be checked bit for bit."""

    index: int
    model: Model
    complexity: int
    train_log_loss: float
    penalized_train_loss: float
    validation_log_loss: float
    validation_log_loss_se: float
    clipped_train: int
    clipped_validation: int
    train_probabilities_sha256: str
    validation_probabilities_sha256: str

    def to_dict(self):
        return {
            "candidate": self.index,
            "model": model_to_dict(self.model),
            "description": describe(self.model.expression),
            "complexity": self.complexity,
            "train_log_loss": self.train_log_loss,
            "penalized_train_loss": self.penalized_train_loss,
            "validation_log_loss": self.validation_log_loss,
            "validation_log_loss_se": self.validation_log_loss_se,
            "clipped": {"train": self.clipped_train, "validation": self.clipped_validation},
            "probabilities_sha256": {
                "train": self.train_probabilities_sha256,
                "validation": self.validation_probabilities_sha256,
            },
        }


class Scorer:
    """Scores expressions on fixed folds. Platt (a, b) is fit on the training fold only;
    validation data affect only validation metrics and the sensitivity check."""

    def __init__(
        self, X_train, y_train, X_val, y_val, feature_names, operator_set, complexity_penalty
    ):
        self.names = check_names(feature_names)
        self.operator_set = operator_set
        self.table = operators(operator_set)
        self.penalty = complexity_penalty
        X_train = np.asarray(X_train, dtype=np.float64)
        X_val = np.asarray(X_val, dtype=np.float64)
        self.train = features(X_train, self.names)
        self.val = features(X_val, self.names)
        self.val_shifted = features(X_val * (1 + PERTURBATION), self.names)
        self.y_train = _labels("y_train", y_train, self.train.rows)
        self.y_val = _labels("y_val", y_val, self.val.rows)
        if self.train.rows < 2 or self.val.rows < 2:
            raise ValueError("training and validation folds need at least two rows each")
        if self.y_train.min() == self.y_train.max():
            raise ValueError("y_train must contain both classes")
        self.inputs = {
            "feature_names": list(self.names),
            "train_rows": self.train.rows,
            "validation_rows": self.val.rows,
            "train_sha256": _digest(X_train, self.y_train),
            "validation_sha256": _digest(X_val, self.y_val),
        }

    def training_scores(self, expr):
        return evaluate(expr, self.train, self.table)

    def score(self, expr, train_scores, index):
        """A Candidate, or the rejection reason (one of REASONS)."""
        if float(np.std(train_scores)) < NEAR_CONSTANT:
            return "near_constant"
        fit = platt_fit(train_scores, self.y_train)
        if fit is None:
            return "platt_failure"
        val_scores = evaluate(expr, self.val, self.table)
        if val_scores is None:
            return "non_finite"
        model = Model(expr, fit[0], fit[1], self.names, self.operator_set)
        p_train = probabilities(train_scores, model.a, model.b)
        p_val = probabilities(val_scores, model.a, model.b)
        train_rows, clipped_train = log_loss_rows(p_train, self.y_train)
        val_rows, clipped_val = log_loss_rows(p_val, self.y_val)
        val_loss = float(np.mean(val_rows))
        shifted = evaluate(expr, self.val_shifted, self.table)
        if shifted is None:
            return "unstable"
        shifted_rows, _ = log_loss_rows(probabilities(shifted, model.a, model.b), self.y_val)
        if abs(float(np.mean(shifted_rows)) - val_loss) > SENSITIVITY * val_loss:
            return "unstable"
        complexity = size(expr)
        train_loss = float(np.mean(train_rows))
        return Candidate(
            index=index,
            model=model,
            complexity=complexity,
            train_log_loss=train_loss,
            penalized_train_loss=train_loss + self.penalty * complexity,
            validation_log_loss=val_loss,
            validation_log_loss_se=float(np.std(val_rows, ddof=1) / math.sqrt(len(val_rows))),
            clipped_train=clipped_train,
            clipped_validation=clipped_val,
            train_probabilities_sha256=_sha(p_train),
            validation_probabilities_sha256=_sha(p_val),
        )


def semantic_fingerprint(scores):
    """SHA-256 of training outputs rounded to multiples of 1e-9 (|scores| <= 1e6)."""
    steps = np.rint(scores / FINGERPRINT_RESOLUTION).astype("<i8")
    return hashlib.sha256(steps.tobytes()).digest()


class BudgetExhausted(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class Archive:
    """Deduplicating, budgeted evaluation ledger.

    A canonical duplicate is not evaluated and not counted as a candidate. Otherwise the
    budgets are checked, the candidate counter increments and training outputs are
    computed. Expressions with one semantic fingerprint form a class represented by its
    simplest evaluated member: the lowest (complexity, canonical key), whatever the
    arrival order. A member that does not beat the representative stops there (a semantic
    duplicate: counted, not re-scored) and shares its outcome; a simpler one is scored and
    replaces it, the old representative becoming the semantic duplicate. Each evaluated
    expression ends as exactly one of: valid, invalid by reason, semantic duplicate. Only
    representatives are valid, so the front and the selection see the simplest members.
    """

    def __init__(self, scorer, max_candidates, max_seconds, clock):
        self.scorer, self.max_candidates, self.max_seconds = scorer, max_candidates, max_seconds
        self.clock = clock
        self.start = clock()
        self.evaluated = 0
        self.duplicates = {"canonical": 0, "semantic": 0}
        self.invalid = dict.fromkeys(REASONS, 0)
        self.representatives = {}  # Candidate.index -> valid representative, scoring order
        self.by_key = {}  # canonical key -> semantic fingerprint, or "non_finite"
        self.by_fingerprint = {}  # fingerprint -> ((complexity, key), outcome)

    @property
    def valid(self):
        """Valid class representatives in scoring order."""
        return list(self.representatives.values())

    def check_time(self):
        if self.clock() - self.start >= self.max_seconds:
            raise BudgetExhausted("max_seconds")

    def outcome(self, key):
        """Outcome of an evaluated canonical key: its class representative's."""
        entry = self.by_key[key]
        return self.by_fingerprint[entry][1] if isinstance(entry, bytes) else entry

    def evaluate(self, expr):
        """(canonical key, Candidate or rejection reason); may raise BudgetExhausted."""
        key = canonical_key(expr)
        if key in self.by_key:
            self.duplicates["canonical"] += 1
            return key, self.outcome(key)
        if self.evaluated >= self.max_candidates:
            raise BudgetExhausted("max_candidates")
        self.check_time()
        self.evaluated += 1
        scores = self.scorer.training_scores(expr)
        if scores is None:
            self.invalid["non_finite"] += 1
            self.by_key[key] = "non_finite"
            return key, "non_finite"
        fingerprint = semantic_fingerprint(scores)
        self.by_key[key] = fingerprint
        rank = (size(expr), key)
        current = self.by_fingerprint.get(fingerprint)
        if current is not None:
            self.duplicates["semantic"] += 1
            if current[0] <= rank:
                return key, current[1]
            self._discount(current[1])  # the old representative is now the duplicate
        outcome = self.scorer.score(expr, scores, self.evaluated)
        self.by_fingerprint[fingerprint] = (rank, outcome)
        if isinstance(outcome, str):
            self.invalid[outcome] += 1
        else:
            self.representatives[outcome.index] = outcome
        return key, outcome

    def _discount(self, outcome):
        if isinstance(outcome, str):
            self.invalid[outcome] -= 1
        else:
            del self.representatives[outcome.index]


@dataclass(frozen=True)
class Individual:
    expr: object
    key: str
    outcome: object
    fitness: float


class _Evolution:
    """Population mechanics. The only source of randomness is self.rng."""

    def __init__(self, config, names, archive):
        self.config, self.names, self.archive = config, names, archive
        self.rng = np.random.Generator(np.random.PCG64(config.seed))
        self.ops = tuple(operators(config.operator_set).values())
        self.oversized = 0

    def individual(self, expr):
        """Fitness: training log loss of the (representative) outcome plus the penalty for
        this expression's own size; invalid outcomes rank last."""
        key, outcome = self.archive.evaluate(expr)
        fitness = math.inf
        if isinstance(outcome, Candidate):
            fitness = outcome.train_log_loss + self.config.complexity_penalty * size(expr)
        return Individual(expr, key, outcome, fitness)

    def fits(self, expr):
        if depth(expr) <= self.config.max_depth and size(expr) <= self.config.max_nodes:
            return True
        self.oversized += 1
        return False

    def fill(self, members, make):
        """Append make() results until the population is full, keeping canonical keys
        distinct; after RETRY_FACTOR * population rejected repeats, repeats are accepted,
        so the work per generation is bounded."""
        keys = {member.key for member in members}
        retries = RETRY_FACTOR * self.config.population
        while len(members) < self.config.population:
            member = make()
            if member is None:
                continue
            if member.key in keys and retries > 0:
                retries -= 1
                continue
            members.append(member)
            keys.add(member.key)
        return members

    def initial(self):
        """Ramped half-and-half over depths 2..max_depth with an operator at the root;
        oversized trees are discarded. Terminates: every cycle of attempts includes
        depth-2 trees (<= 3 nodes)."""
        config = self.config
        depths = range(2, config.max_depth + 1)
        attempt = 0

        def make():
            nonlocal attempt
            level, full = depths[attempt % len(depths)], (attempt // len(depths)) % 2 == 0
            attempt += 1
            tree = random_tree(
                self.rng, self.ops, self.names, config.constant_range, level, full, True
            )
            return self.individual(simplify(tree)) if self.fits(tree) else None

        return self.fill([], make)

    def tournament(self, population):
        picks = self.rng.integers(len(population), size=self.config.tournament)
        best = population[int(picks[0])]
        for i in picks[1:]:
            if population[int(i)].fitness < best.fitness:
                best = population[int(i)]
        return best

    def child(self, population):
        """Crossover, mutation or reproduction; an oversized child is discarded, counted and
        replaced by its (first) parent."""
        config = self.config
        draw = self.rng.random()
        parent = self.tournament(population)
        if draw < config.crossover_rate:
            expr = crossover(self.rng, parent.expr, self.tournament(population).expr)
        elif draw < config.crossover_rate + config.mutation_rate:
            kind = MUTATIONS[int(self.rng.integers(len(MUTATIONS)))]
            expr = mutate(
                self.rng,
                parent.expr,
                kind,
                self.ops,
                self.names,
                config.constant_range,
                config.max_depth,
            )
        else:
            return parent
        return self.individual(simplify(expr)) if self.fits(expr) else parent

    def next_generation(self, population):
        ranked = sorted(range(len(population)), key=lambda i: (population[i].fitness, i))
        elites, keys = [], []
        for i in ranked:
            if len(elites) == self.config.elitism or math.isinf(population[i].fitness):
                break
            if population[i].key not in keys:
                elites.append(population[i])
                keys.append(population[i].key)
        return self.fill(elites, lambda: self.child(population))


def _summary(generation, population, evaluated):
    valid = [ind for ind in population if not math.isinf(ind.fitness)]
    fitness = [ind.fitness for ind in valid]
    return {
        "generation": generation,
        "valid": len(valid),
        "best_penalized_train_loss": min(fitness) if fitness else None,
        "median_penalized_train_loss": float(np.median(fitness)) if fitness else None,
        "best_validation_log_loss": (
            min(ind.outcome.validation_log_loss for ind in valid) if valid else None
        ),
        "candidates_evaluated": evaluated,
    }


def pareto_front(candidates):
    """Non-dominated (complexity, validation log loss) candidates, simplest first. A more
    complex candidate must improve on every simpler one by more than FRONT_TOLERANCE
    (affine variants of one expression differ only by rounding after Platt scaling); at
    equal complexity the earliest-scored of equal losses is kept."""
    best = {}
    for candidate in candidates:
        current = best.get(candidate.complexity)
        if current is None or candidate.validation_log_loss < current.validation_log_loss:
            best[candidate.complexity] = candidate
    front = []
    for complexity in sorted(best):
        loss = best[complexity].validation_log_loss
        if not front or loss < front[-1].validation_log_loss - FRONT_TOLERANCE:
            front.append(best[complexity])
    return tuple(front)


def select(front):
    """Simplest front member with validation loss <= min + SE, where SE is the standard
    error of the per-row validation log loss of the minimizer."""
    if not front:
        return None, {
            "rule": SELECTION_RULE,
            "min_validation_log_loss": None,
            "standard_error": None,
            "threshold": None,
        }
    minimizer = front[-1]
    threshold = minimizer.validation_log_loss + minimizer.validation_log_loss_se
    chosen = next(c for c in front if c.validation_log_loss <= threshold)
    return chosen, {
        "rule": SELECTION_RULE,
        "min_validation_log_loss": minimizer.validation_log_loss,
        "standard_error": minimizer.validation_log_loss_se,
        "threshold": threshold,
    }


@dataclass(frozen=True)
class SearchResult:
    """Complete search record. `elapsed_seconds` is wall time on the injected clock and is
    deliberately excluded from `to_dict` (it would break byte-identical replays)."""

    config: SearchConfig
    inputs: dict
    stop_reason: str
    generations_completed: int
    candidates_evaluated: int
    valid: int
    duplicates: dict
    invalid: dict
    oversized_discarded: int
    history: tuple
    front: tuple
    selected: Candidate | None
    selection: dict
    elapsed_seconds: float

    @property
    def reproducible(self):
        return self.stop_reason != "max_seconds"

    def to_dict(self):
        config = self.config
        return {
            "engine": ENGINE_VERSION,
            "config": config.to_dict(),
            "config_hash": config.config_hash(),
            "seed": config.seed,
            "inputs": {**self.inputs, "feature_names": list(self.inputs["feature_names"])},
            "budgets": {
                "generations": config.generations,
                "max_candidates": config.max_candidates,
                "max_seconds": config.max_seconds,
            },
            "stop_reason": self.stop_reason,
            "reproducible": self.reproducible,
            "generations_completed": self.generations_completed,
            "counters": {
                "candidates_evaluated": self.candidates_evaluated,
                "valid": self.valid,
                "duplicates": dict(self.duplicates),
                "invalid": dict(self.invalid),
                "oversized_discarded": self.oversized_discarded,
            },
            "history": [dict(entry) for entry in self.history],
            "front": [candidate.to_dict() for candidate in self.front],
            "selected": None if self.selected is None else self.selected.to_dict(),
            "selection": dict(self.selection),
            "validation_metrics_selection_biased": True,
        }


def search(X_train, y_train, X_val, y_val, feature_names, config, *, clock=time.monotonic):
    """Run one bounded search on a training fold and a later validation fold.

    The validation fold drives selection, so the selected expression's validation metrics
    are selection-biased; only an untouched holdout evaluated once by the caller is not.
    """
    if not isinstance(config, SearchConfig):
        raise TypeError("config must be a SearchConfig")
    scorer = Scorer(
        X_train,
        y_train,
        X_val,
        y_val,
        feature_names,
        config.operator_set,
        config.complexity_penalty,
    )
    archive = Archive(scorer, config.max_candidates, config.max_seconds, clock)
    evolution = _Evolution(config, scorer.names, archive)
    history, completed = [], 0
    try:
        population = evolution.initial()
        history.append(_summary(0, population, archive.evaluated))
        for generation in range(1, config.generations + 1):
            archive.check_time()
            population = evolution.next_generation(population)
            history.append(_summary(generation, population, archive.evaluated))
            completed = generation
        stop_reason = "generations"
    except BudgetExhausted as exc:
        stop_reason = exc.reason
    front = pareto_front(archive.valid)
    selected, selection = select(front)
    return SearchResult(
        config=config,
        inputs=scorer.inputs,
        stop_reason=stop_reason,
        generations_completed=completed,
        candidates_evaluated=archive.evaluated,
        valid=len(archive.valid),
        duplicates=dict(archive.duplicates),
        invalid=dict(archive.invalid),
        oversized_discarded=evolution.oversized,
        history=tuple(history),
        front=front,
        selected=selected,
        selection=selection,
        elapsed_seconds=clock() - archive.start,
    )
