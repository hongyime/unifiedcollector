# Recovery security-alignment handoff

The running recovered stack mounts `/home/bryan/Projects/.worktrees/collector-production-recovery` for application source, while the live browser extension loads `/home/bryan/Projects/unifiedcollector/extension`.

`main` contains the completed security-hardening merge and extension version `1.23.126`; the recovery branch remains on `1.23.125` plus its recovery commits. Do not freeze another release from the mixed state. Prepare and verify the integration in a separate worktree first, preserving the active recovered stack and its private evidence. Current runtime is healthy: PostgreSQL, Beeper, SpiderFoot, the guard, and staged collectors are running; recon admission remains halted. The recovery worktree has an unrelated untracked `Z:/` directory—do not alter it.
