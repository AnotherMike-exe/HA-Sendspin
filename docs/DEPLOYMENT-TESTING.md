# Deployment & Test Loop

How a change gets from this repo onto the live Home Assistant instance and how
logs come back — **without granting SSH**, and **without any install step that a
normal HACS user wouldn't perform**.

---

## Design constraints

These are requirements, not preferences. Don't optimise them away.

1. **Install path must be identical to a normal HACS install.** If testing needs
   a terminal command on the HA box, we are no longer testing what users get.
   HACS does the file copy; we never `scp` a `custom_components/` directory.
2. **No SSH to the HA instance.** No shell, no filesystem access.
3. **Log retrieval must be autonomous.** Claude pulls logs unattended; nobody
   babysits the loop.

---

## The mechanism: a Long-Lived Access Token

Home Assistant's REST API covers everything the loop needs:

| Need | Endpoint |
|---|---|
| Pull logs | `GET /api/hassio/core/logs?lines=N` (see below) |
| Raise log verbosity, no restart | `POST /api/services/logger/set_level` |
| Trigger the HACS download | `POST /api/services/update/install` |
| Restart HA | `POST /api/services/homeassistant/restart` |
| Confirm entities materialised | `GET /api/states` |
| Readiness probe after restart | `GET /api/` |

All authenticate with `Authorization: Bearer $HA_TOKEN`.

### Which log endpoint — verified on the live instance

**`/api/error_log` returns 404 on this instance** (HA 2026.8.1), even though the
HA REST API docs still list it. Auth is not the problem: `/api/`, `/api/config`,
and `/api/states` all return 200 with the same token.

The working source is the **Supervisor journal proxy**, available because this is
an HA OS / Supervised install (`hassio` is loaded):

```
GET /api/hassio/core/logs?lines=500
```

| | `/api/hassio/core/logs` | `/api/error_log` |
|---|---|---|
| This instance | ✅ 200 | ❌ 404 |
| Line limiting | `?lines=N` (default 100) | none |
| Survives an HA restart | ✅ journal-backed | ❌ rotated |
| Requires | Supervisor (HA OS / Supervised) | any install |

`scripts/ha_probe.sh` tries the Supervisor proxy first and falls back to
`/api/error_log`, so it also works on a Container/Core install where the proxy
doesn't exist. `GET /api/hassio/core/logs/boots/0` scopes to the current boot if
you ever need that.

HA writes ANSI colour escapes into the journal; the script strips them so output
is readable and greppable.

### Creating the token

HA UI → click your user (bottom-left) → **Security** tab → **Long-lived access
tokens** → *Create token*. Shown once. Store it as `HA_TOKEN` in `.env`
(gitignored) or the macOS keychain.

### What this actually grants — read before creating it

A long-lived access token is **admin-equivalent over the REST API**. It can call
any service on the instance, including `homeassistant.restart` and anything
exposed by other integrations.

It is meaningfully narrower than SSH — no shell, no arbitrary filesystem reads,
no access to other services on that host, no ability to alter HA outside its own
API surface — but it is **not** a read-only scoped credential, and pretending
otherwise would be the wrong basis for a decision. Home Assistant does not
currently offer a scoped or read-only token type.

**Practical mitigations:**

- Create the token under a **dedicated HA user** rather than your daily admin
  account, so it can be revoked independently.
- Revocation is instant and self-service from the same Security tab.
- Never commit it. `.env` is gitignored; verify with `git status` before the
  first commit.

> ⚠️ **Still unverified**: whether a *non-admin* token can reach the Supervisor
> log proxy. The token in use is admin, so this hasn't been tested. If a
> non-admin token is rejected, the dedicated-user benefit reduces to "separately
> revocable" rather than "less privileged". Test and record the result here.

---

## The loop

```
  ┌─ 1. Claude commits, pushes, tags a release ──────────┐
  │      gh release create vX.Y.Z                        │
  └──────────────────────┬───────────────────────────────┘
                         ▼
  ┌─ 2. HACS sees the new release ───────────────────────┐
  │      Surfaces update.sendspin_update in HA           │
  └──────────────────────┬───────────────────────────────┘
                         ▼
  ┌─ 3. Claude triggers the download ────────────────────┐
  │      POST /api/services/update/install               │
  │      ← exactly what "Redownload" in the HACS UI does │
  └──────────────────────┬───────────────────────────────┘
                         ▼
  ┌─ 4. Claude restarts HA and waits for readiness ──────┐
  │      POST /api/services/homeassistant/restart        │
  │      then poll GET /api/ until 200                   │
  └──────────────────────┬───────────────────────────────┘
                         ▼
  ┌─ 5. Claude pulls logs and inspects state ────────────┐
  │      GET /api/error_log · GET /api/states            │
  └──────────────────────────────────────────────────────┘
```

Steps 3–5 are fully autonomous. Use `scripts/ha_probe.sh` — see below.

### One-time manual setup

Unavoidable, and correct — it's the same thing any user does once:

1. HACS → ⋮ → **Custom repositories** → add the repo URL, category *Integration*.
2. Download it once through the HACS UI.
3. Restart HA and add the integration via **Settings → Devices & Services**.

After that, the loop above runs unattended.

---

## Helper script

`scripts/ha_probe.sh` wraps the endpoints. Requires `HA_BASE_URL` and `HA_TOKEN`.

```bash
./scripts/ha_probe.sh logs              # full error log
./scripts/ha_probe.sh logs sendspin     # log lines matching a pattern
./scripts/ha_probe.sh debug             # set custom_components.sendspin → debug
./scripts/ha_probe.sh states            # sendspin-related entities
./scripts/ha_probe.sh update            # trigger the HACS update entity
./scripts/ha_probe.sh restart           # restart, then wait for readiness
./scripts/ha_probe.sh cycle             # update → restart → wait → logs
```

---

## Gotchas

- **Logs survive restarts** on this instance, because the Supervisor proxy reads
  the systemd journal rather than a rotating file. This is a genuine advantage
  over `/api/error_log` — you can restart and still read what happened before it.
  (On a Container/Core install falling back to `/api/error_log`, the old caveat
  applies: pull logs *before* restarting or they're gone.)

- **`logger.set_level` survives without a restart, but not across one.** Set
  debug level *after* the restart in step 4, or add a permanent `logger:` block
  to the HA `configuration.yaml`:

  ```yaml
  logger:
    default: warning
    logs:
      custom_components.sendspin: debug
      aiosendspin: debug
  ```

- **HACS polls for updates on its own schedule.** The `update` entity may not
  appear the instant a release is tagged. If step 3 finds no update entity,
  that's usually latency, not failure. There is no reliable force-refresh
  service in current HACS — **verify what's available in the installed version
  and record it here.**

- **A restart is genuinely required.** Python module changes are not picked up by
  a config-entry reload. Don't skip step 4 and then debug stale code.

- **Tag a real release per test cycle.** HACS prefers releases; without one it
  tracks the default branch, which makes "which code is actually installed"
  ambiguous during rapid iteration. Cheap tags (`v0.1.0-test.3`) are fine.

---

## Status

**Partially verified against the live instance (HA 2026.8.1, 2026-08-11).**

Confirmed working:
- Token auth against `/api/`, `/api/config`, `/api/states`
- Log retrieval via `/api/hassio/core/logs?lines=N`, ANSI stripped
- `./scripts/ha_probe.sh logs`, `logs <pattern>`, and `states`

Not yet exercised:
- `update` / `restart` / `cycle` — these bounce the live instance, so they were
  left alone during scaffolding
- HACS update-entity naming: `ha_probe.sh` assumes `update.sendspin_update`.
  The real entity id won't exist until the repo is added to HACS. Override with
  `HA_UPDATE_ENTITY` in `.env` once it does.
- Whether a non-admin token can read the Supervisor log proxy

### Note on existing Sendspin entities

`states` already returns entities on this instance:

```
media_player.esparagus_hifi_1_esparagus_hifi_1_sendspin_player   idle
media_player.plum_sendspin                                       idle
button.plum_sendspin_favorite_current_song                       unknown
```

These come from **other** integrations (Music Assistant and/or Plum-Audio), not
this one. Expect entity-id collisions once this integration creates its own —
worth deciding early whether to namespace, and worth remembering that `states`
output is not evidence that *this* integration is working.
