# "I changed it and nothing happened"

Every entry below is a place where the thing you edited is **not** the thing that's running. They
share one root cause: state that the box can write is never authoritative for what the host does,
and containers freeze parts of their config at creation. Work down the table before assuming a bug.

| you edited | why it didn't take | what makes it take |
| --- | --- | --- |
| `foldyard.toml` / `foldyard.local.toml` | the host reconciles from the copy it **adopted**, outside the mount — your edit is inert by design | the human answers the adopt/revert/ignore prompt at the next `fy up`/`fy host`, or runs `fy config adopt`. `fy config status` / `fy config diff` show where you are |
| a block you deleted from `foldyard.toml` | `foldyard.local.toml` wins the merge and may still declare it (that file is gitignored, so it's invisible in `git status`) | `fy config widenings` names the file behind each one; a local `disabled = true` is how one person opts out of a block the team keeps |
| a compose file, inside a worktree | compose runs from the **primary checkout**, not your worktree | the edit has to reach the primary checkout |
| a compose file, anywhere | a running container keeps the command + env it was CREATED with | `fy up` (recreates on a config change) — a `restart` is not enough |
| an env var in the stack's env file | same: baked at container creation | recreate the service |
| posture (`fy mode …`) | setting is host-only; from the box it refuses | ask the human, then re-check `fy mode` |
| an egress allow / the wall | grants live in the host's home | `fy allow add <host>` on the host |
| a code file, expecting hot reload | the watcher may not see host-side writes across the VM's shared filesystem — but **edits made in the box do** propagate | make the edit in the box (you're already there); if a host-side edit was the trigger, touch the file from here |
| foldyard's own source (vendored consumers only) | a running supervisor holds the code it started with | the next host-side `fy up` bounces it and adopts the new code |
| a bundled skill / docs | they ship inside the installed package | reinstall foldyard, or run from the source checkout |

## Diagnosing, in order

1. `fy config status` — is the config the host runs the one you edited?
2. `fy state` — desired vs observed, per tier (files, daemons, capability, stack, box). It exits
   non-zero on drift, so it's the fastest "is anything out of sync" check.
3. `fy doctor` — one line per check with its fix; the row that's warning usually is the answer.
4. `fy ps` / `fy logs <svc>` — is the container even the one you think? Check when it was created.

## The general rule

If a change would let the **box** widen what the **host** does — its credentials, its egress, its
capture — assume it needs a host-side act, and say which one. That's not an obstacle to route
around; it's the property that makes it safe to run an agent here with permissions skipped.
