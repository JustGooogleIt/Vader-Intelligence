"""Vader Command Center v1: a loopback-only operator dashboard over bounded read
projections of the archive, the operations state, the research orchestration store and
the Command Center's own run/command store, plus bounded farm controls.

Specification: `docs/specs/command-center-v1.md`. The server never writes the archive or
the orchestration store; commands are durable rows applied by the controller process.
"""
