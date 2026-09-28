# Vader Intelligence

Read `docs/handoffs/baseline.md` before changing code. Use the final checkpoint SHA
given to the operator as the starting point for both parallel jobs; do not start
implementation from the minimal `main` branch.

- Each session uses its own branch, checkout/worktree, virtual environment and test
  database. Preserve ignored local data and unrelated changes.
- Settlement owns new settlement/mapping modules, additive migrations, tests and
  documentation. Operations owns CLI wrappers, scheduling, monitoring and backups.
- During parallel work, operations must not edit core storage, the shared CLI,
  collector/parser/mapping modules, migrations or database schema. Shared-interface
  proposals belong in each stream's integration notes for later coordinated work.
- No trades, paid model calls, force pushes, destructive migrations or merges into
  main are implied by a workstream task.
- Use installed engineering skills at the recorded pin; preserve local skill edits.
  Do not upgrade unrelated tooling. Never commit credentials, databases, logs,
  environments, build output or downloaded provider payloads.
- Offline tests and live evidence must remain clearly distinguished. An empty
  eligible live universe is inconclusive. Run lint and relevant tests after fixes.
