# Architecture Overview

Living document. Update as the codebase evolves.

**Status**: discovery, adoption, source selection, now-playing, cover art and
transport are implemented and verified against real hardware. Per-speaker volume
for a speaker another server holds is not — see
[OPEN-QUESTIONS §8](OPEN-QUESTIONS.md).

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

**Home Assistant is the dialer, in both directions it participates in.** A
Sendspin server dials players; players listen. So HA-as-server dials speakers,
and nothing on the network ever dials HA.

```
                        LAN (mDNS, browse only)
                               │
        ┌──────────────────────┼───────────────────────┐
        │                      │                       │
  [Speakers]            [Plum-Audio units]      [Music Assistant]
  _sendspin._tcp        _sendspin-server._tcp    (a Sendspin server)
  :8928 listening       :8927 + mesh API :5001
        ▲                      │
        │ we dial them         │ we GET/POST over HTTP
        │ (adoption)           │ (source list + routing)
        │                      ▼
   ┌────┴──────────────────────────────────────┐
   │  custom_components/sendspin               │
   │                                           │
   │  discovery.py   ← HA core zeroconf        │
   │  config_flow.py → hub entry + subentries  │
   │  server_host.py → in-process SendspinServer (source-less, silent)
   │  mesh.py        → optional Plum federation tier
   │  coordinator.py → server events (push) + mesh poll (pull)
   │  media_player.py→ one entity per speaker  │
   └────────────────┬──────────────────────────┘
                    ▼
            [Home Assistant core]
       entity registry · automations · services
```

**Why a server and not a controller client.** A controller-role client can send
transport commands and observe only the group it currently occupies. No wire
message lists groups, lists players, or moves a player between groups. Adoption
and routing exist solely as in-process Python on a `SendspinServer`.

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

### 3.3. Coordinator (`coordinator.py`)

A `DataUpdateCoordinator` used **without a poll interval**. The source of truth
is the in-process server's *event stream*, not a controller websocket. Mesh
state is polled separately on a 5s timer, because Plum aggregates on 2s and
faster buys nothing.

**Invariant: the endpoint set is the adopted set, never the connected set.** A
speaker that drops off goes `unavailable`; it never disappears. Pruning on
disconnect would make a network hiccup indistinguishable from the user removing
a speaker.

Server/player reachability maps onto **entity availability** — this is what lets
HA automations trigger on a stream coming online or a player dropping off the
mesh natively, with no polling script.

On disconnect the coordinator marks data unavailable and schedules a reconnect
rather than tearing down entities, so a brief mesh blip does not orphan entity
registry entries.

### 3.4. Media player platform (`media_player.py`)

**One durable entity per physical speaker**, not per stream. Streams come and
go constantly; anchoring entities to them would churn the entity registry and
discard the user's renames, areas and icons on every reconnect.

**Live** streams instead populate each speaker's `source_list`, and
`select_source` is the routing verb. Several speakers selecting the same source
*is* the group — Sendspin's own semantics — so no grouping feature is
advertised.

"Live" is load-bearing. A Plum unit publishes every *configured input* as a
source, so the list is filtered on `active or streaming` — an AirPlay endpoint
with no sender is not somewhere a speaker can usefully be put. `None` is always
offered, and whatever is currently selected is pinned even once it stops being
live, so a stream ending under a routed speaker cannot leave the entity
reporting a value absent from its own options. See
[OPEN-QUESTIONS §7](OPEN-QUESTIONS.md#7-hardware-findings--m1-gate-2026-08-11),
which records the measurement that reversed the original decision to list
everything.

Which source a speaker is on comes from matching its `group_id` against each
source's, because `player_ids` is empty on every unit of a real mesh. That match
is also the only way a routing change made elsewhere — Music Assistant, the Plum
GUI — becomes visible here.

### 3.5. Controller links (`legacy_client.py`)

**A hand-written protocol client**, not the library's. `aiosendspin` 9.1.0's
client always initiates the 8.0 Noise handshake and cannot fall back —
`allow_unencrypted` exists only on the server — so it cannot talk to any
Sendspin server in the field. Music Assistant and Plum-Audio units alike close
the socket on `client/init`. The older protocol is plain JSON over a websocket,
so this speaks it directly, which also decouples the controller role from the
library version permanently.

This is the **only** source of now-playing. A controller observes just the group
it occupies, so:

- Against a Plum unit, a client id of `ctrl:<source_id>:<nonce>` asks that unit
  to place the controller in the named source's group. Deterministic, and used
  for every source one of our speakers is on.
- Against any other server, there is no such hook — Music Assistant leaves a
  controller in its own solo group reporting nothing. `switch` cycles a
  controller through that server's *playing* groups, which is the only way in.
  Hunting is continuous but rate-limited, and never applied to a targeted link.

Attribution — deciding which speaker a link's now-playing belongs to — is, in
order: the speaker is on a mesh source this link observes; the mesh says a
server holds it and this link *is* that server; or exactly one observed server
is playing. Two servers playing the **same** track is one answer, not an
ambiguity: a server feeding another's input puts the same audio on both.

### 3.6. Services (`services.py`)

The **adoption lifecycle only**: `adopt_player`, `release_player`,
`reclaim_player`. Routing is not a service — it is `media_player.select_source`.

No service takes a `player_id`. They target Home Assistant devices, and the one
raw identifier accepted anywhere is the listener URL, on `adopt_player`, where
by definition no device exists yet.

---

## 4. Data Stores

**Three, and two of them hold secrets.** An earlier version of this document
said "None", which was wrong and is worth stating plainly because it is a
security-relevant claim.

| Store | Contents | Why it must persist |
|---|---|---|
| `.storage/sendspin.identity` | The server's **X25519 private key** | The public half *is* the `server_id` peers see. Regenerating it makes Home Assistant look like a brand new server to every speaker it has ever paired with. |
| `.storage/sendspin.pairings.json` | Pairing records (**PSKs**) | Written by aiosendspin's own `FileServerPairingStore`, which does all its file I/O off the event loop. |
| `.storage/sendspin.players` | Per-speaker names, last client id, last dial URL | Keeps a speaker's good name while it is offline — which is exactly when the name stops being visible on the network. |

The adopted-speaker set itself is not a data store: it is config subentries, so
it is the user's consent record rather than integration state.

---

## 5. External Integrations / APIs

| Dependency | Purpose | Method |
|---|---|---|
| `aiosendspin` | Sendspin **server** role — adoption, groups, routing primitives | Python library, pinned `==9.1.0` |
| (hand-written) | Sendspin **controller** role — now-playing, artwork, transport | `legacy_client.py`, plain JSON over a websocket |
| Plum-Audio mesh API | Stream list and routing | HTTP, optional, auto-detected |

**Source enumeration is Plum-only, by protocol necessity.** Sendspin defines no
way to discover what a server is offering: mDNS carries only `path` and `name`,
no message in the set is an inventory, `group/update` describes the single group
the connection occupies, and a `switch` group-walk reaches only playing groups
and reports them unnamed (measured against all three servers on the LAN — one
anonymous group each). Plum's `/api/mesh/view` is the only enumerator that
exists. On a mesh-free network the integration therefore publishes no
`source_list`; adoption, presence, volume and now-playing all still work.
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

- **Secrets at rest.** An X25519 private key and pairing PSKs are written to
  `.storage` in plaintext, as with all Home Assistant storage. They are covered
  by HA backups, which is desirable — losing the key invalidates every pairing
  — and means a backup carries them.
- **LAN-local, unauthenticated.** Sendspin control traffic is cleartext `ws://`;
  there is no TLS at any protocol version. The integration runs its server with
  `allow_unencrypted=True`, without which no device currently on a typical
  network can connect at all.
- **The Plum mesh API has no authentication whatsoever**, and requests without
  an `Origin` header bypass its CORS policy entirely. Anything on the LAN can
  reroute audio. That is a property of that API, not a choice made here.
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
- **Repository URL**: <https://github.com/AnotherMike-exe/HA-Sendspin> (MIT)
- **Primary Contact**: Plum Solutions
- **Date of Last Update**: 2026-08-11

---

## 11. Glossary

- **Sendspin**: synchronised multi-room audio protocol. Spec:
  <https://www.sendspin-audio.com/spec/>
- **Controller / client role**: the Sendspin role that *directs* playback. This
  project does **not** implement it — no server on a pre-8.0 network will accept
  it (OPEN-QUESTIONS §7). It hosts a source-less **server** instead.
- **Frozen URL / dial URL**: the endpoint's permanent identity versus where it
  answers today. Kept strictly apart so DHCP cannot orphan an entity.
- **Listener URL**: a Sendspin server's addressable endpoint. Used here as the
  stable entity identity key.
- **Roam**: moving a player between servers. Not a distinct operation in this
  integration — it is simply selecting a source that lives on another unit.
- **HACS**: Home Assistant Community Store — distribution channel for custom
  integrations.
- **hassfest**: Home Assistant's manifest/structure validator.
- **MA**: Music Assistant. Has native Sendspin support; this project deliberately
  duplicates that surface to remove the MA dependency.
