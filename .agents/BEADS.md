## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Never work directly over master, create a worktree for your work.

## Definition of DONE (CLOSED)
- A ticket can be marked as done (closed), once it has been merged.

### Preconditions
- All quality gates defined for the project must be green, don't flag issues as pre-existing, you take ownership and fix any quality gate that is not green.
- The work should have been reviewed and approved (two pair of eyes principle) by a human or another agent with no prior context about the task.
- Clean: Remove leftover comments, dead code resulted from the implementation, orphan worktrees, etc. Tidy up when you finish.
- Handoff: If there is folloup work discovered during the implementation, you must create a beads ticket for it.