# Bounded symbolic regression engine specification v1

Status: implementation specification for roadmap item C (engine core), 2026-10-18.
Integration with the dataset builder, experiment registry and evaluator is done in the
same PR by the integrator; the engine itself works on in-memory arrays.

## 1. Interface

`src/vader_intelligence/symbolic/` (pure Python + numpy):

```python
@dataclass(frozen=True)
class SearchConfig:
    operator_set: str = "ops-v1"          # versioned operator table
    max_depth: int = 5
    max_nodes: int = 25
    population: int = 200
    generations: int = 30
    max_candidates: int = 20000            # total expressions evaluated (hard cap)
    max_seconds: float = 60.0              # wall budget (monotonic)
    tournament: int = 5
    crossover_rate: float = 0.7
    mutation_rate: float = 0.25
    elitism: int = 5
    complexity_penalty: float = 0.002      # per node, added to training log loss
    constant_range: tuple = (-2.0, 2.0)
    seed: int = 0

def search(X_train, y_train, X_val, y_val, feature_names, config) -> SearchResult
```

`X_*` float64 arrays (n × k), `y_*` int arrays in {0,1}. NaN in X is allowed only if
the dataset declares a missing-value treatment; the engine itself treats NaN outputs as
invalid. The engine never receives holdout data.

## 2. Expressions

- Prefix-tree JSON: `{"op": "add", "args": [...]}`, `{"var": "mid_logit"}`,
  `{"const": 0.25}` (constants stored as decimal strings with 12 significant digits for
  stable serialization).
- Operator set `ops-v1`: binary `add`, `sub`, `mul`, `pdiv` (protected:
  `a / b` if `|b| > 1e-6` else `a`), `min`, `max`; unary `neg`, `abs`, `plog`
  (`log(|x| + 1e-6)`), `psqrt` (`sqrt(|x|)`), `tanh`, `square` (clipped to 1e6). All
  intermediate values clipped to [-1e6, 1e6]; any NaN/inf → candidate invalid.
- Complexity = node count. Depth/size limits enforced at generation, crossover and
  mutation (oversized children are discarded and counted).

## 3. Probability interface

Raw score `s = expr(x)`; probability `p = sigmoid(a·s + b)` where `(a, b)` is a Platt
fit (Newton/IRLS, L2 1e-6, bounded iterations) on the **training** fold only. The stored
model is `{expression, a, b, operator_set, feature_names}`; `predict_proba(X)`
recomputes deterministically. Probabilities are clipped to [1e-6, 1 − 1e-6] for loss
computation only; the clipping count is reported.

## 4. Search

Ramped half-and-half initialization; tournament selection on penalized training loss;
subtree crossover; point, subtree and hoist mutation; elitism. Every evaluated
expression increments the candidate counter; the run stops at whichever budget is hit
first (generations, `max_candidates`, `max_seconds`) and records which.

Deduplication: canonical form (commutative operands sorted, constants rounded to 6
significant digits, trivial identities folded: `x+0`, `x*1`, `x*0`, `neg(neg(x))`) plus
a semantic fingerprint (hash of training-fold outputs rounded to 1e-9). Duplicates are
counted, not re-scored.

Invalid/unstable: invalid (non-finite) outputs, near-constant output (std < 1e-9),
Platt fit failure, or extreme sensitivity (relative change in validation loss > 0.5 when
inputs are perturbed by 1e-6 relative) → rejected with reason; counts by reason reported.

## 5. Selection and history

`SearchResult` contains: config and its hash, seed, budgets and the binding stop reason,
candidates evaluated, duplicates, invalid counts by reason, per-generation best/median
penalized training loss and best validation loss, the Pareto front (complexity vs
validation log loss) and the selected expression with its selection rule (default: the
simplest expression within one standard error of the minimum validation log loss on the
front). Validation is chronological (the caller provides time-ordered folds); the
validation fold is used for selection, so validation metrics of the selected expression
are *selection-biased* and must be labelled as such; only a separate, untouched holdout
evaluated once gives an unbiased estimate.

## 6. Determinism and replay

Same inputs + config + seed → identical `SearchResult` JSON (wall-time budget aside: when
`max_seconds` binds, the result records it and is not claimed reproducible). `replay(model,
X)` returns identical probabilities to those computed during search.

## 7. Tests required

Protected operators at boundaries; serialization round trip; canonicalization and
dedup; budget stops (candidate cap, generations, time); determinism with fixed seed;
recovery of a planted relation on synthetic data (e.g. y ~ Bernoulli(sigmoid(2·x0 − x1)))
better than a constant model; invalid expression rejection; no holdout argument exists.
