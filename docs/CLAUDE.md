# CLAUDE.md — HA-Sendspin

> Project memory for Claude Code. Rules and context that apply to every session
> in this repo. Project-level guidance here overrides the global `~/.claude/CLAUDE.md`.

## Project Overview

**HA-Sendspin** is a Home Assistant **custom integration** (domain: `sendspin`)
that discovers and controls Sendspin servers and players on the LAN, generically
and independently of Music Assistant.

### Key Features
- **Discovery** — Sendspin servers/players found via mDNS, using HA core's
  `zeroconf` integration (declared in `manifest.json`).
- **Media player entities** — one per active stream/group: source, transport
  controls, metadata and artwork via the Sendspin metadata role.
- **Routing services** — add/remove a player to/from a group; roam a player
  between servers.
- **Availability as a first-class signal** — server/player presence maps onto
  entity availability, so automations trigger on a stream coming online or a
  player dropping off the mesh natively, with no polling script.

### Why this exists
Music Assistant already provides discovery, `media_player` entities, and
grouping for Sendspin. This is **deliberate duplication** of that surface. The
reason to build it anyway: control and routing **without a dependency on MA being
installed or running**. Getting the Sendspin slice today means bringing in all of
MA — its player-source ecosystem, config, and update cadence. This removes that
dependency. Smaller surface, one job.

**Accepted cost, going in**: this is a second independent Sendspin
controller-role implementation with no upstream to lean on. Spec drift is this
project's problem to track, not MA's.

### Project Context
- **Stage**: scaffold — structure only, no behaviour implemented
- **Team Size**: solo
- **Priority Focus**: correctness over surface area; the identity and arbitration
  traps in `docs/OPEN-QUESTIONS.md` are the real risk

---

## Claude Code Preferences

### Workflow Mode
- **Plan mode is encouraged.** This project has unresolved design questions with
  real consequences (see `docs/OPEN-QUESTIONS.md`). For anything touching
  discovery, entity identity, or routing semantics, plan first and get agreement
  before writing code. Direct implementation is fine for mechanical work —
  translations, test scaffolding, lint fixes, docs.
- **Subagents are encouraged.** Use them freely for parallel exploration:
  reading HA core integrations as prior art, surveying `aiosendspin`, or sweeping
  Plum-Audio for reference implementations. Prefer a subagent over pulling large
  reference files into the main context — return the conclusion, not the dump.
- **Testing Approach**: TDD where it fits. Config flow and coordinator logic are
  well suited to it; entity plumbing less so.

### Communication Style
- Concise. Assume competence — skip basic explanations.
- Interrupt early on ambiguity rather than guessing at architecture decisions.

---

## Critical Project Rules

These encode traps this project **will** hit. They are not style preferences.

1. **Key entities on the listener URL, never the client id.** A Sendspin client's
   id is not stable across the two identity views. Keying on it orphans entities
   on rename or reconnect. Plum-Audio hit this directly.
2. **Intra-server re-route and cross-server roam are different operations.** Do
   not collapse them into one service. Group membership is fixed at
   `start_stream()` — a player added to a live group **joins silent** unless the
   stream is refreshed. Roam is reconnect-based and is subject to the unresolved
   multi-server arbitration gap.
3. **Controller role only.** This integration never renders audio and never
   *advertises* over mDNS — it only browses. Plum-Audio's `mesh/avahi.py` also
   advertises; it is a reference, not code to copy.
4. **The manifest `zeroconf` service type and `const.ZEROCONF_SERVICE_TYPE` must
   stay in sync.** They are duplicated by necessity.
5. **Read `docs/OPEN-QUESTIONS.md` before building routing or identity code.**
   Two items there are marked blocking.

---

## Technology Stack

- **Language**: Python 3.13 (match the HA release being targeted)
- **Platform**: Home Assistant custom integration (not an add-on — see below)
- **Key dependency**: `aiosendspin` — Sendspin controller/client role
- **Distribution**: HACS
- **Testing**: `pytest` + `pytest-homeassistant-custom-component`
- **Lint/format**: `ruff`
- **CI**: GitHub Actions — `hassfest`, HACS action, pytest

### Not an add-on — and why
HA **add-ons** are Supervisor-managed Docker containers (`config.yaml`,
Dockerfile, s6/bashio) that only install on HA OS/Supervised, and **cannot create
entities**. This project has no audio pipeline of its own, so there is nothing to
containerise, and it needs entities. Custom integration is the correct form.

Consequence: **the Binhex Docker conventions in the global standards do not apply
to this repo.** No `/config` `/data` `/media` volumes, no `PUID`/`PGID`/`UMASK`,
no compose file. Don't add them.

---

## Naming Convention

**`snake_case`** — a deliberate deviation from the house PascalCase standard.

Python's ecosystem dictates it, and Home Assistant enforces it structurally: HA's
loader imports platform modules by exact filename (`media_player.py`,
`config_flow.py`), and the integration directory name must equal the `domain` in
`manifest.json`. PascalCase filenames would fail to load.

- Modules/files/directories: `snake_case`
- Functions/variables: `snake_case`
- Classes: `PascalCase`
- Constants and env vars: `UPPER_SNAKE_CASE` (unchanged from the house standard)

Shell scripts in `scripts/` follow the house PascalCase for function names.

---

## Project Structure

```
custom_components/sendspin/   # The integration — dir name == manifest domain
  __init__.py                 # Entry setup/unload, service registration
  manifest.json               # Domain, zeroconf, requirements, version
  const.py                    # DOMAIN, service names, config keys
  config_flow.py              # User + zeroconf discovery flows
  coordinator.py              # Controller websocket -> entity state (push)
  discovery.py                # Parses mDNS records from HA core
  media_player.py             # One entity per active stream/group
  services.yaml               # Routing services
  icons.json                  # Service icons (HA 2024.2+ location)
  strings.json                # Source strings
  translations/en.json        # Literal strings — no [%key%] refs in customs
  brand/                      # HACS-required icon.png
docs/                         # All docs except README
tests/                        # pytest-homeassistant-custom-component
scripts/ha_probe.sh           # Live-instance log/deploy helper
_resources/                   # Dev references — NOT in git
```

`_resources/Notes/` holds the original project outline.

---

## Deployment & Test Loop

Full detail in **[DEPLOYMENT-TESTING.md](DEPLOYMENT-TESTING.md)**. Summary:

Testing happens against a live HA instance **over the REST API with a
long-lived access token — no SSH**. The install path is deliberately identical to
a normal HACS install; never copy files onto the HA box by hand, because then we
are not testing what users get.

```bash
./scripts/ha_probe.sh cycle      # update -> restart -> debug logging -> logs
./scripts/ha_probe.sh logs sendspin
./scripts/ha_probe.sh states
```

Requires `HA_BASE_URL` and `HA_TOKEN` (see `.env.example`). **`HA_TOKEN` is
admin-equivalent over the API and must never be committed.**

Claude may run the read-only subcommands (`logs`, `states`) autonomously.
`update`, `restart`, and `cycle` restart the user's live home automation system —
**confirm before running these** unless the user has said otherwise in-session.

### Gotchas
- **`/api/error_log` returns 404 on the target instance** (HA 2026.8.1) despite
  still being in the HA REST docs. Logs come from the Supervisor journal proxy,
  `GET /api/hassio/core/logs?lines=N`. `ha_probe.sh` handles this and falls back
  to `/api/error_log` for Container/Core installs. Don't "fix" it back.
- Journal-backed logs **survive restarts**, so you can restart and still read
  what happened before it.
- `logger.set_level` does not survive a restart. Re-apply after, or add a
  permanent `logger:` block to HA's `configuration.yaml`.
- A restart **is** required for Python changes — a config-entry reload will not
  pick them up.
- **`states` already shows Sendspin entities from other integrations** (Music
  Assistant / Plum-Audio) on this instance. Their presence is not evidence that
  this integration works. Expect entity-id collisions.

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest                       # run tests
ruff check .                 # lint
ruff format .                # format
```

`pytest-homeassistant-custom-component` pins a specific HA version. Bump it
deliberately — an unpinned bump silently changes which HA release is validated.

---

## Git

- `git pull --rebase` (alias `git pr`) — linear history.
- Atomic, well-described commits.
- Tag a release per deploy-test cycle; HACS prefers releases over branch
  tracking, and without one "which code is installed" is ambiguous.

---

## Outstanding TODOs in the scaffold

Search for `TODO` — the notable ones:

- `manifest.json` — `documentation`, `issue_tracker`, `codeowners` are
  placeholders. **hassfest and the HACS action will fail until these are real.**
- `manifest.json` / `const.py` — the mDNS service type `_sendspin._tcp.local.` is
  a **guess**. Confirm against the Sendspin spec and Plum-Audio's `mesh/avahi.py`.
- `manifest.json` — `aiosendspin>=0.0.0` needs a real version pin.
- `custom_components/sendspin/brand/` — needs a real `icon.png` (binary, not
  scaffolded).
- Every module raises `NotImplementedError`. Nothing works yet by design.

---

## Resources

- Sendspin spec: <https://www.sendspin-audio.com/spec/>
- `aiosendspin`: <https://github.com/Sendspin/aiosendspin>
- MA Sendspin support (prior art for the entity/grouping model):
  <https://www.music-assistant.io/player-support/sendspin/>
- HA developer docs: <https://developers.home-assistant.io/>
- **Plum-Audio prior art** (separate repo): `backend/scripts/mesh/avahi.py`,
  `backend/scripts/mesh/router.py`, `backend/scripts/sendspin_server.py`,
  `docs/HARD-WON-LESSONS.md`, `docs/UPSTREAM-AIOSENDSPIN.md` §1
