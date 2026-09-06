# Acceptance evidence

These records capture specific completed checks, their source revision, and their limits. A successful local exercise does not establish remote PR/CI behavior or replace an outstanding review. Summaries exclude browser capabilities, launch tokens, credentials, and conversation bodies; source hashes identify the original local records.

- [Pocket Tasks](pocket-tasks-workflow-2026-09-06.json): built by managed Codex agents, with independent blast-radius review, an automatic performance specialist, real task artifacts, and native recovery.
- [CSV Brief](csv-brief-recovery-2026-09-06.json): the second managed app, with provider and terminal failures, supervisor restart, explicit stop/resume, fresh replacement, stale-request rejection, completed-work restart, and process cleanup.
- [Web workbench](web-workbench-2026-09-06.json): browser, native-conversation, task-viewer, and packaging checks. The workbench was developed directly in this repository; it was not the second app built through ctlrm.

The original web-worktree evidence and Pocket Tasks store were copied into a verified private archive under the main checkout's `.scratch/evidence-backups/`, outside `/tmp`. CSV Brief's original records and both source exports are also under the main checkout's `.scratch/`. Those untracked records remain private; the committed summaries above are the durable repository evidence. Runtime snapshots preserve their original absolute worktree/interpreter identities and are evidence backups, not relocatable running sessions. They do not contain the provider's external credential or native-conversation directory.
