# Follow-up work

## Remote secondmate provisioning and updates

- **What:** Add explicit, human-authorized provisioning and guarded updates for remote Mate secondmate installations.
- **Why:** Manual setup and version maintenance across multiple hosts can drift, leaving missing tools or incompatible runtimes that prevent reliable dispatch.
- **Context:** The first remote-dispatch version will require a manually prepared remote account with SSH access, Mate, Pi, Herdr, Treehouse, credentials and a disposable or project repository. The agreed architecture uses an independent remote Mate home, approvals through the primary, remote-owned execution state and a separate transport process per remote home. Start from `plans/remote-dispatch.md` and the eventual readiness/protocol checks. Provisioning must not copy credentials, overwrite unrelated files, or stop live workers without explicit coordination. Updates must check protocol compatibility and preserve durable requests, receipts, approvals and reports; do not silently replace running processes.
- **Depends on / blocked by:** Implementing the remote secondmate protocol and readiness checks, defining supported version compatibility, and passing automated fixtures plus an actual disposable SSH smoke test. Remote dispatch is not implemented yet.

Cross-host task migration and automatic failover are intentionally not scheduled; add only when a concrete need justifies their ownership and duplicate-execution risks.
