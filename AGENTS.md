# Working agreements

Check user changes with `git status --short`, then read the current handoff in [HANDOFF.md](HANDOFF.md).
Use the documentation index in [README.md](README.md) to select only the details needed for the task.

- Choose implementation and task order autonomously within the requested scope; finish work that can proceed independently.
  Do not seek reconfirmation for routine code, documentation, or synthetic tests. Isolate blockers and continue other work.
- The primary path is raw tick replay. LOB/one-second bar conversion is not a default prerequisite.
- Preserve `sampledata/Daily_baseline`, `sampledata/old_data`, original capture data, and user changes.
- While capture is running, limit work to lightweight code/docs, small synthetic tests, and brief observations.
  Defer collector restarts/replacement, additional OCX logins, bulk production DB processing, full backtests, and load tests to off-hours validation.
- Do not infer clean shutdown, completed persistence, or data correctness from completion claims in docs, PIDs, logs, or file existence alone.
  Check code and relevant tests; keep unknown values unknown.
- Validate changes with small relevant tests. Report synthetic validation separately from production deployment and real-data validation.
- On completion, update HANDOFF with current status and next steps, then commit locally. The user handles pushes.
  Keep each detailed contract in its owning document and link new documents from README.
- Respond to the user in Korean unless requested otherwise. Keep shared human-facing documentation in Korean.
