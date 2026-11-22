"""Research orchestration: durable task store, coordinator, pod workers and candidates.

The orchestration database is the source of truth; tmux is optional observation only.
Workers are local subprocesses for fault isolation and resource bounding. They are not a
security sandbox: pods are restricted to declarative operations from the approved
registry, run with a scrubbed environment and a private working directory, and never
receive live credentials, risk limits, evaluator configuration, holdout data or archives.

Import submodules directly (`pods.store`, `pods.coordinator`, ...). This module stays
import-light because every worker process imports it.
"""
