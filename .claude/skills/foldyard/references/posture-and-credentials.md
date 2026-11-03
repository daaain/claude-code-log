# Posture and credentials — how the box gets access, and why you can't grant it

The yard's resting state is **zero secrets**: no key files, no token env vars, no credential
helpers. Nothing to lift, so nothing to leak. Access is a *posture* the human declares on the host,
and it reaches you as capability, never as a secret you hold.

## Axes and rungs

Posture is a set of independent **axes**, each at a **rung**. Which axes exist is per project —
`fy mode` prints them with a one-line blurb per rung; there is no fixed list to memorise. Typical
shapes: an off/on injector for one API, or a ladder like `off → logs → sa → user` where each rung
grants more.

- **Rung 0 is always the secretless one.** An axis at rest costs nothing and grants nothing.
- **High-privilege rungs are TTL-bound.** "Act as me" rungs expire (default an hour) and revert to
  the default rung on their own. If something worked earlier and now 401s, check `fy mode` first —
  a lapse looks exactly like a broken credential.
- `fy mode` also renders **DEGRADED**: the rung is set, but the host's capability probe says the
  chain behind it isn't working right now (an expired grant, revoked access). That's the difference
  between "not asked for" and "asked for, not currently working".

## How a credential reaches you (and doesn't)

The real token is minted **on the host** and injected into your request in flight by the egress
proxy. The box holds a dummy value at most. So:

- reading the env var gives you a placeholder — that's expected, not a misconfiguration;
- the access only exists while the axis is on, and only against the host it's scoped to;
- a fully compromised box can *use* the access during that window but can never *hold* it.

Some services in the stack instead get an identity from a local metadata emulator — same idea:
the container is handed short-lived capability, not a key.

## What to do when you need access

You cannot set posture from the box (`fy mode <axis>=<rung>` refuses here — the authoritative state
is in the host's home, outside the mount). So:

1. Run `fy mode` and read the axis blurbs; work out the exact rung you need.
2. Ask for it explicitly, with the reason and the command:
   *"To read staging logs I need `fy mode gcp=logs` on your Mac (expires in an hour)."*
3. If a **secret** is missing rather than a posture — the host may need to capture it once
   (`fy box up` prompts on a TTY, storing it host-side at 0600). `fy doctor` on the host names
   which one.

## Reading the current grant surface

`fy config widenings` inventories what this project's config asks the host to allow: which hosts
are exempt from traffic capture, where each mechanism delivers a credential (armed *and* declared
but not yet armed), which agent prompts are shared with the team vs personal, and any config key
that reads as a control but is no longer honoured.

Depth: `fy docs modes`, `fy docs security`, `fy docs adr-0005` (why axes are data), `fy docs
adr-0007` (injection at the proxy), `fy docs adr-0008` (keyless agent auth).
