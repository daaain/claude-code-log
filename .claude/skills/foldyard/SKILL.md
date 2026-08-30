---
name: foldyard
description: Orient yourself in a foldyard dev box and drive it — the host/box split, what the box structurally cannot do, posture modes for credentials, egress and the proxy, worktrees, and why a config or compose change might not have taken effect. Use whenever `fy`/`foldyard` commands appear, when something needs a credential or network access it doesn't have, when a change "doesn't take", or when asked to work on the dev environment itself.
---

# Working in (and on) a foldyard dev box

Foldyard runs this project inside a **rootless VM that mounts only this repo**. You are almost
certainly *in the box* — a container in that VM — and the person you're working with is on the
**host** (their Mac). That split explains nearly everything below.

Check where you are: `fy mode` prints the posture and says which side it's reading from.
`fy verify` proves the boundary (it exits non-zero if any of it is false).

## The four facts that will save you the most time

1. **You cannot push.** No credential to the git remote reaches the box, by design. Read and
   commit locally; the human pushes. Don't try to work around it — there is nothing to find.
2. **You hold no real secrets, and can't grant yourself any.** Posture state lives in the host's
   home, outside the mount. `fy mode` *shows* posture anywhere; *setting* it is host-only.
3. **Your egress goes through the host's proxy** and may be allow-listed or walled. A blocked
   host is a decision, not a bug — ask; don't route around it.
4. **The engine socket you have is real** and bounded to this VM. `docker`/`podman` commands
   work and act on the live dev stack — including anything a test suite's mocks let slip.

## Ask the tool, don't guess

Foldyard is introspectable, and its own output is always current — prefer it over any summary
(including this one):

| question | command |
| --- | --- |
| what can this environment do right now? | `fy mode` (posture), `fy state` (desired vs observed) |
| what's broken / missing? | `fy doctor` — one line per check, each with its fix |
| is the isolation intact? | `fy verify` |
| what does this verb do? | `fy --help`, `fy <verb> --help` |
| the manual, offline, by topic | `fy docs` — then `fy docs <topic>` |
| what does the config let the host do? | `fy config widenings` |

`fy docs` serves **this install's** documentation, so it can't drift from the code you're running.
Start with `fy docs quickstart`, `fy docs modes`, `fy docs networking`, `fy docs security`; the
ADRs (`fy docs adr-0001` …) carry the reasoning behind each design decision.

## Daily verbs

`fy up | ps | logs [svc] | shell | down` drive the compose stack; `fy box shell | ps` the dev box
itself; `fy verify` proves the cage. A consumer repo usually wraps these in its own `just`
recipes — check the repo's justfile and CLAUDE.md before reaching for `fy` directly.

## When you need something you don't have

**A credential** (a cloud token, an API key): you can't grant it. Name the exact command the human
should run on their Mac and why. Postures are per-axis — `fy mode` lists the axes this project
declares and their rungs, and high-privilege rungs expire on a timer by design. Detail:
[posture-and-credentials.md](references/posture-and-credentials.md).

**A host through the network wall**: `fy allow add <host> --level session` — **on the host**. Tell
them the hostname and what needs it. Detail:
[egress-and-capture.md](references/egress-and-capture.md).

**A tool in the box**: it belongs in the project's `foldyard.toml` (`[[box.tools]]`) so it survives
a box rebuild, not in an ad-hoc `apt install` that the next `fy box down` throws away. The
`bootstrap-devbox` skill covers growing that config from inside the box.

## "I changed it and nothing happened"

The most common class of confusion here. Full table:
[when-a-change-doesnt-take.md](references/when-a-change-doesnt-take.md). The three big ones:

- **`foldyard.toml`** — the host runs the copy it **adopted**, not your working tree. Your edit is
  inert until the human answers the prompt at the next `fy up`/`fy host` (or runs `fy config
  adopt`). Check with `fy config status`; see what changed with `fy config diff`.
- **Compose files** — read from the **primary checkout**, so an edit inside a worktree changes
  nothing until it reaches that checkout.
- **A running container** — keeps the command and env it was created with. A config change needs a
  recreate (`fy up`), not a restart.

## Working on foldyard itself

Read `fy docs` first — it's the installed version's own manual. The source lives at
<https://github.com/twistco/foldyard>. Don't clone it into the box to "check how it works": you'd
get a different version than the one running here, which is the exact confusion this design goes
out of its way to prevent. The supported way to develop against it is the **co-dev mount**
(`fy docs adr-0020`), which mounts the real checkout and installs it editable.
