# Architecture Overview

Living document. Update as the codebase evolves.

**Scaffold status**: structure and intent are documented; no behaviour is
implemented yet. Sections marked *planned* describe design intent, not code.

---

## 1. Project Structure

```
HA-Sendspin/
├── custom_components/
│   └── sendspin/              # The integration. Directory name == manifest domain.
│       ├── __init__.py        # Config entry setup/unload, service registration
│       ├── manifest.json      # Domain, zeroconf records, requirements, version
│       ├── const.py           # DOMAIN, service names, config keys (UPPER_SNAKE_CASE)
│       ├── config_flow.py     # User + zeroconf discovery flows
│       ├── coordinator.py     # Controller websocket → entity state (push)
│       ├── discovery.py       # Parses mDNS records handed over by HA core
│       ├── media_player.py    # One entity per active stream/group
│       ├── services.yaml      # Group add/remove, roam
│       ├── icons.json         # Service icons (HA 2024.2+ location)
│       ├── strings.json       # Source strings
│       ├── translations/      # en.json (literal strings — no [%key%] refs)
│       └── brand/             # HACS-required icon.png (see brand/README.md)
├── docs/
│   ├── ARCHITECTURE.md        # This document
│   ├── CLAUDE.md              # Claude Code project memory (symlinked to root)
│   ├── OPEN-QUESTIONS.md      # Unresolved design blockers — read before building
│   ├── DEV-SETUP.md           # Environment setup
│   └── QUICK-REFERENCE.md     # Plum Solutions standards cheat sheet
├── tests/                     # pytest-homeassistant-custom-component
├── scripts/                   # Automation helpers
├── .github/workflows/         # hassfest + HACS validation, pytest
├── _resources/                # Dev references — NOT in git
├── hacs.json                  # HACS distribution manifest
├── pyproject.toml             # ruff + pytest config
├── requirements-dev.txt       # Dev/test deps (runtime deps live in manifest.json)
├── README.md
└── CLAUDE.md                  # Symlink → docs/CLAUDE.md
```

---

## 2. High-Level System Diagram

```
                        LAN (mDNS / Avahi)
                               │
              ┌────────────────┴─────────────────┐
              │                                  │
    [Sendspin Server A]                [Sendspin Server B]
       │  listener URL                     │  listener URL
       │                                   │
       │  controller websocket             │
       └──────────────┬────────────────────┘
                      │  (push state)
                      ▼
        ┌─────────────────────────────┐
        │  custom_components/sendspin │
        │                             │
        │  discovery.py  ← HA core zeroconf
        │       │                     │
        │  config_flow.py → ConfigEntry (unique_id = listener URL)
        │       │                     │
        │  coordinator.py  (aiosendspin controller role)
        │       │                     │
        │  media_player.py            │
        └───────┬─────────────────────┘
                │
                ▼
        [Home Assistant core]
          entity registry · automations · services
```

**Direction of control**: this integration is *client/controller role only*. It
never renders audio and never advertises itself over mDNS — it only browses.

---

## 3. Core Components

### 3.1. Discovery (`discovery.py`) — *planned*

**Responsibility**: turn zeroconf records into `SendspinDiscovery` values.

HA core's `zeroconf` integration performs the browsing; declaring the service
type in `manifest.json` is sufficient to receive discoveries. This module does
**not** run its own browser and does **not** advertise.

Plum-Audio's `backend/scripts/mesh/avahi.py` is a working reference for the
D-Bus plumbing and the TXT record shape — **reference, not code to copy**: it
also advertises, which this project does not need.

### 3.2. Config flow (`config_flow.py`) — *planned*

Two paths: manual listener-URL entry, and a zeroconf-triggered discovery flow
(the same first-class pattern Sonos and Cast use).

Unique id is the **listener URL**. See ARCHITECTURE §7 and
[OPEN-QUESTIONS §2](OPEN-QUESTIONS.md#2-entity-identity--🔴-blocking).

### 3.3. Coordinator (`coordinator.py`) — *planned*

A `DataUpdateCoordinator` used **without a poll interval**. The controller
websocket pushes state; the coordinator calls `async_set_updated_data` from the
websocket callback.

Server/player reachability maps onto **entity availability** — this is what lets
HA automations trigger on a stream coming online or a player dropping off the
mesh natively, with no polling script.

On disconnect the coordinator marks data unavailable and schedules a reconnect
rather than tearing down entities, so a brief mesh blip does not orphan entity
registry entries.

### 3.4. Media player platform (`media_player.py`) — *planned*

One entity per active stream/group: source, transport controls, and metadata /
artwork via the Sendspin metadata role.

### 3.5. Routing services — *planned*

Registered in `__init__.py`, described in `services.yaml`. Intra-server re-route
and cross-server roam are **separate services with different semantics** — see
[OPEN-QUESTIONS §3](OPEN-QUESTIONS.md#3-scope-of-routing--🟡-needs-a-design-pass).

---

## 4. Data Stores

None. State is ephemeral and lives in the coordinator; the only persistence is
Home Assistant's own config entry and entity registry storage.

---

## 5. External Integrations / APIs

| Dependency | Purpose | Method |
|---|---|---|
| `aiosendspin` | Sendspin controller/client role — stream control, group add/remove/roam | Python library (pinned in `manifest.json`) |
| HA core `zeroconf` | mDNS discovery | Manifest `zeroconf` key → discovery flow |

**Relevant `aiosendspin` calls** (per Plum-Audio prior art in
`backend/scripts/sendspin_server.py` and `backend/scripts/mesh/router.py`):
`connect_to_client`, `reclaim_client_for_playback`, `group.add_client`,
`group.remove_client`.

---

## 6. Deployment & Infrastructure

**Not containerised.** Distributed as a HACS custom integration, installed into
`<ha-config>/custom_components/sendspin/`.

No Docker, no compose, no Binhex volume conventions apply here — this project has
no container of its own. (This is a deliberate departure from the Plum Solutions
default; see [OPEN-QUESTIONS §4](OPEN-QUESTIONS.md#4-packaging--🟢-decided-revisit-only-if-forced).)

**CI**: GitHub Actions — `hassfest` and the HACS action validate the manifest and
repo layout; pytest runs the test suite.

---

## 7. Security Considerations

- **LAN-local, unauthenticated.** Sendspin control traffic is local; the
  integration inherits whatever the Sendspin spec provides. No credentials are
  currently stored in the config entry.
- **Identity is not authentication.** The listener URL is used as a stable
  identity key, not as a trust boundary. Anything on the LAN that can reach the
  listener can control it — this is a property of the protocol, not of this
  integration.

---

## 8. Development & Testing Environment

- **Setup**: [DEV-SETUP.md](DEV-SETUP.md)
- **Testing**: `pytest` via `pytest-homeassistant-custom-component` (pins a
  specific HA version — bump deliberately)
- **Code quality**: `ruff` (lint + format); `hassfest` for manifest validity

---

## 9. Future Considerations / Roadmap

- Resolve the arbitration and identity blockers in
  [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md) before building routing.
- Possible companion add-on **only if** discovery proves to need host networking
  that a plain integration cannot obtain.
- Track Sendspin spec drift independently — there is no upstream to inherit
  fixes from.

---

## 10. Project Identification

- **Project Name**: HA-Sendspin (integration domain: `sendspin`)
- **Repository URL**: [TODO — org and repo name undecided]
- **Primary Contact**: Plum Solutions
- **Date of Last Update**: 2026-08-11

---

## 11. Glossary

- **Sendspin**: synchronised multi-room audio protocol. Spec:
  <https://www.sendspin-audio.com/spec/>
- **Controller / client role**: the Sendspin role that *directs* playback, as
  opposed to the player role that renders audio. This project implements the
  former only.
- **Listener URL**: a Sendspin server's addressable endpoint. Used here as the
  stable entity identity key.
- **Roam**: moving a player from one Sendspin server to another. Reconnect-based;
  distinct from intra-server group membership changes.
- **HACS**: Home Assistant Community Store — distribution channel for custom
  integrations.
- **hassfest**: Home Assistant's manifest/structure validator.
- **MA**: Music Assistant. Has native Sendspin support; this project deliberately
  duplicates that surface to remove the MA dependency.
