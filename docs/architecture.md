# Architecture (MVP)
- Mattermost channel is the human/agent interface.
- Switch room carries shared messages, task context, instructions, and handoffs.
- Codex agents are configured with role-specific instructions.
- RepoRescue Python package defines task state and transition rules.
- Git worktree/branch isolates proposed changes.
- pytest provides reproducible verification.
- Human approval is a hard gate before PR/merge/deploy.

## Safety boundaries
- No production credentials in agent context.
- No automatic merge/deploy.
- Use a disposable demo repository first.
- Require human confirmation for destructive commands.
