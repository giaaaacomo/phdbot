# PHDBOT agent guidance

## Objective

Keep PHDBOT useful, trustworthy, and recoverable while minimizing unnecessary
model/GPU work. Optimize for correct, searchable opportunities per unit of
compute and for time to first useful search result, not for emptying queues or
maximizing processed-row counts.

## Working protocol

- Work in atomic blocks: diagnose, change, verify, and leave a runnable state.
- Preserve user changes. Never discard or reset the dirty worktree.
- Do not stop a user-started process unless explicitly authorized or required
  to prevent data loss. Record why any autonomous stop was necessary.
- Before migrations, bulk relabeling, or destructive index repair, verify a
  restorable backup. Ordinary code and UI edits do not require a new backup.
- Prefer targeted tests and canaries while iterating; run the full unit suite,
  Ruff, and mypy before a production deploy.
- Change one behavioral variable per evaluation. Independent UI changes may be
  batched.
- Use aggregate SQL and focused logs rather than dumping large datasets.
- Do not poll long-running stages. Use durable pipeline checkpoints and the
  persistent scheduler, then inspect at meaningful milestones or on failure.
- Use subagents only for independent, bounded work with a concrete deliverable;
  stop them when that deliverable is complete.
- Keep user updates short: material state changes, blockers, launches, and the
  final handoff.

## Review and indexing principles

- Never force a verdict without supporting evidence. Abstention is safer than
  a fabricated decision.
- Also avoid spending deep-review compute on records that lack evidence,
  freshness, or a reachable source. Route those to evidence retry/deferred.
- Treat deep review as a targeted adjudicator, not the default processor for
  the entire review backlog.
- Review is an optional precision accelerator, never a prerequisite for making
  a safe provisional lead searchable. A large `review` count is not a human
  task list: expose reversible uncertainty and spend compute only where it can
  materially change a decision.
- Measure automatic resolution, audited error rate, technical failure rate,
  and GPU seconds per valid searchable opportunity.
- Keep verified and provisional searchability distinct. Provisional records
  must be clearly labelled, filterable, reversible, and excluded by hard rules
  such as explicit closure, elapsed deadline, broken extraction, or confirmed
  non-opportunity.
- User reports must hide an item immediately and feed an auditable feedback
  workflow; never create domain-wide rejection rules from one report.

## Safe operator handoff

- `done`: leave the run untouched and record its ID.
- `failed` with an explicit retry/resume message: use Resume, never Start.
- other failures: preserve the run and error for diagnosis.
- `running` at the end of an energy window: Stop is checkpoint-preserving; use
  Resume during the next window.
- Before leaving work unattended, update the ignored local
  `docs/CURRENT_STATE.md` (copy `docs/CURRENT_STATE.example.md` when absent).
