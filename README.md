# HA-Sendspin

> A Home Assistant custom integration for discovering and controlling Sendspin devices on your LAN.

Exposes Sendspin servers and players to Home Assistant as `media_player` entities
with transport controls, metadata, and grouping — **without requiring Music
Assistant**. If you want HA-native Sendspin routing today, the alternative is
installing all of MA to get the Sendspin slice. This is that slice, on its own.

> **Status: scaffold.** Structure and design docs only — no working
> functionality yet. Not installable in a useful state.

---

## Features

- 🔍 **Automatic discovery** of Sendspin servers and players via mDNS
- 🔊 **A media player entity per stream/group** — source, transport controls,
  metadata and artwork
- 🔀 **Routing services** — add/remove players from groups, roam players between
  servers
- 📶 **Presence as availability** — automations can trigger on a stream coming
  online or a player dropping off the mesh, with no polling script

## Requirements

- Home Assistant 2024.12.0 or newer
- One or more Sendspin servers/players on the same LAN
- [HACS](https://hacs.xyz/) for installation

## Installation

**Via HACS** (custom repository, until/unless this is listed by default):

1. HACS → ⋮ → **Custom repositories**
2. Add this repository's URL, category **Integration**
3. Download **Sendspin**, then restart Home Assistant
4. **Settings → Devices & Services → Add Integration → Sendspin**

Discovered servers on your network are offered automatically — you may not need
step 4 at all.

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — system design and key decisions
- [Open Questions](docs/OPEN-QUESTIONS.md) — unresolved design blockers
- [Deployment & Test Loop](docs/DEPLOYMENT-TESTING.md) — how changes reach a live instance
- [Development Guide](docs/DEV-SETUP.md) — environment setup
- [Quick Reference](docs/QUICK-REFERENCE.md) — standards cheat sheet
- [CLAUDE.md](CLAUDE.md) — Claude Code project memory

## Tech Stack

- **Language**: Python 3.13
- **Platform**: Home Assistant custom integration (`custom_components/sendspin`)
- **Protocol**: [Sendspin](https://www.sendspin-audio.com/spec/) via
  [`aiosendspin`](https://github.com/Sendspin/aiosendspin), controller role only
- **Distribution**: HACS

Not containerised — this integration has no audio pipeline of its own, so there
is nothing to ship in a container.

## Relationship to Music Assistant

[Music Assistant](https://www.music-assistant.io/player-support/sendspin/)
already supports Sendspin natively and does it well. This project deliberately
duplicates that surface for one reason: **removing the MA dependency.** If you
already run MA, you probably want MA. If you want Sendspin control without
adopting MA's whole ecosystem, you want this.

## License

[TODO — not yet decided]

---

**Repository**: [TODO] · **Maintainer**: Plum Solutions
