# Modelling library specification v1 (roadmap B3, archive-independent part)

Status: implementation specification, 2026-10-19. The dataset builder, feature registry
and research registry are built separately (by the integrator) and call this library
with in-memory arrays. This library never touches an archive, the network or a clock.

## Principles

1. Every fitted transformation is fit on the training rows it is given and serialized.
   Nothing global, nothing fit on evaluation data. Callers pass train and evaluation
   arrays separately; no function accepts both and "decides".
2. Artifacts are plain JSON (no pickle, no joblib): parameters as decimal strings with 17
   significant digits, canonical key order, SHA-256 of the canonical bytes as identity.
   Loading verifies the hash and the format version; an unknown version or a mismatch is
   an explicit error (`ArtifactError`), never a fallback.
3. Inference needs only numpy and reproduces the training library's predictions (parity
   is asserted at export time and recorded in the artifact).
4. Determinism: same inputs + config + seed → byte-identical artifact JSON (on one
   platform/numpy build). All randomness via `numpy.random.Generator(PCG64(seed))` or an
   explicit `random_state=seed`.
5. Missing values are explicit: features declare a treatment; NaN where the treatment is
   `forbid` is an error.

## Package `src/vader_intelligence/models/`

| Module | Contents |
|---|---|
| `preprocess.py` | `FeatureSpec(name, missing="median_indicator"\|"zero_indicator"\|"forbid", scale=True)`; `Preprocessor.fit(X, specs)` → learns medians, means, standard deviations (std floor 1e-12 → column marked constant and dropped from scaling), indicator columns appended in a fixed order; `transform(X)`; `output_names`; `to_dict()/from_dict()` |
| `logistic.py` | `fit_logistic(Z, y, *, C, max_iter=1000, tol=1e-8, seed)` via `sklearn.linear_model.LogisticRegression(solver="lbfgs", penalty="l2")`; returns `{"coef": [...], "intercept": ...}`; `logit(params, Z)` numpy |
| `gbm.py` | `fit_gbm(Z, y, *, learning_rate, max_iter, max_leaf_nodes, min_samples_leaf, l2_regularization, max_bins=255, seed)` via `HistGradientBoostingClassifier(early_stopping=False, random_state=seed, categorical_features=None)`; export every tree (from the fitted predictors' node arrays: `feature_idx, num_threshold, missing_go_to_left, left, right, is_leaf, value`) plus the baseline raw prediction; `raw_score(params, Z)` numpy traversal (left iff `x <= num_threshold`, NaN follows `missing_go_to_left`); parity with sklearn `decision_function` asserted ≤ 1e-9 at export and stored as `parity_max_abs_error` |
| `calibration.py` | `fit_platt(scores, y, *, l2=1e-6)` (Newton, bounded iterations); `fit_isotonic(probabilities, y)` via `sklearn.isotonic.IsotonicRegression(out_of_bounds="clip", y_min=1e-6, y_max=1-1e-6)` exported as threshold arrays, inference with `numpy.interp`; `identity`; all serializable; `apply(cal, raw_scores_or_probs)` |
| `metrics.py` | `brier`, `log_loss` (natural log; probabilities clipped to [1e-6, 1-1e-6] for the loss only, clip count returned), `calibration_table(p, y, bins=10)` (equal-width; count, mean p, mean y), `ece`, `grouped_bootstrap(metric, p, y, groups, *, n=1000, seed)` → (estimate, low, high) resampling groups, `paired_bootstrap(metric, p_a, p_b, y, groups, ...)` → difference CI |
| `cv.py` | `chronological_folds(dates, groups, *, n_folds, min_train_fraction=0.4, embargo_days=1)` expanding window: each fold's train rows strictly precede its test rows by at least the embargo; a group never spans train and test (assert); returns index arrays; refuses if a fold would be empty |
| `search.py` | `grid_search(family, grid, X, y, dates, groups, specs, *, n_folds, seed, max_configs, deadline_seconds, clock)` → for every config: fit preprocess+model on each fold's train only, predict that fold's test; returns per-config mean/SE log loss, the selected config (lowest mean log loss; tie → simpler/first), and **out-of-fold** predictions of the selected config (for calibration fitting without touching validation). Records configs tried, budget, stop reason |
| `artifact.py` | `ModelArtifact` (format `model-artifact-v1`): `family` (`logistic`\|`gbm`\|`market_baseline`\|`symbolic`), `feature_names`, `feature_specs`, `preprocessor`, `params`, `calibration`, `training` (dict supplied by caller: dataset manifest hash, split, config, seed, code hash, dependency versions), `parity`; `to_json()` canonical, `sha256`, `save(dir)` write-once (refuse overwrite of different content), `load(path, expected_sha256=None)`; `predict_proba(artifact, X)` = preprocess → family score → calibration; `check_inputs(artifact, feature_names)` raises `ArtifactError("feature mismatch")` |
| `baselines.py` | parameter-free "models" over a named input column: `market_mid` (probability = the column, clipped to (0,1)), `constant` (0.5); same artifact interface so they are scored identically |

`symbolic` artifacts are produced by the separate symbolic engine; `artifact.py` must
accept a `symbolic` family whose params are the engine's model dict and whose scoring is
delegated to a function registered at import (`register_family(name, raw_score_fn)`), so
the integrator can plug it in without editing this module.

## Tests (`tests/models/`, synthetic data only, no network, total < 60 s)

Preprocessor fit on train only (changing test rows never changes fitted params);
missing-value treatments and indicators; constant columns; `forbid` raises. Logistic and
GBM: export/import round trip, numpy inference parity with sklearn (GBM including NaN
routing), determinism with seed. Calibration: Platt/isotonic round trip, monotonicity,
clipping. Metrics: hand cases (Brier of 0.5 = 0.25, log loss of 0.5 = ln 2), clip counts,
grouped bootstrap determinism with seed and group resampling (all rows of a group move
together). CV: no group in both train and test; embargo respected; chronological order;
refusal on too few rows. Search: budgets (max_configs, deadline via injected clock),
out-of-fold predictions never come from a model that saw that row. Artifact: hash
verification, overwrite refusal, unknown format/family refusal, feature mismatch refusal,
registered family plug-in.
