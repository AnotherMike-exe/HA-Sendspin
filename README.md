# HA-Sendspin

> A Home Assistant custom integration for discovering and controlling Sendspin
> speakers on your LAN — **without requiring Music Assistant**.

Home Assistant becomes a Sendspin server of its own, so it can adopt speakers,
control them, and move them between the streams already running on your
network. If you want HA-native Sendspin routing today, the alternative is
installing all of Music Assistant to get the Sendspin slice. This is that
slice, on its own.

> **Status: v0.1.0 — early, and honest about it.** Adoption, presence, volume
> and stream selection work and are covered by tests. **Metadata, artwork,
> progress and transport do not** — see [what doesn't work yet](#what-doesnt-work-yet).

---

## What works

- 🔍 **Automatic discovery** of Sendspin servers and speakers via mDNS. Nothing
  is ever adopted automatically — taking a speaker means taking it *from*
  whatever currently has it, so that is always your decision.
- 🔊 **One `media_player` per physical speaker**, durable across reconnects and
  IP changes. Volume and mute where the speaker reports them.
- 🔀 **Streams as a source dropdown.** Available streams populate each speaker's
  source list; picking one hands the speaker to the unit running that stream.
  Several speakers on the same source *is* a group — that is Sendspin's own
  model, so there is no separate grouping control.
- 📶 **Presence as availability**, so automations can trigger on a speaker
  dropping off the mesh with no polling script.
- 🤝 **It declines to fight.** If another Sendspin server takes a speaker back,
  the integration stops dialling and tells you, rather than starting a
  tug-of-war that degrades both. `sendspin.reclaim_player` overrides that when
  you actually want the speaker.

## What doesn't work yet

- **No metadata, artwork, progress or transport controls.** These require a
  controller connection to the server that is playing, and **no Sendspin server
  running a pre-8.0 release will accept one** from the current library — the
  handshake is rejected outright. This affects Music Assistant and Plum-Audio
  units alike. See [OPEN-QUESTIONS §7](docs/OPEN-QUESTIONS.md).
- **Home Assistant plays no audio of its own.** It routes and controls; it is
  not a source.
- **The stream list needs a Plum-Audio unit.** No Sendspin server exposes a way
  for an outsider to enumerate its streams, so the source dropdown comes from
  Plum-Audio's mesh API. Without one, the integration still adopts and controls
  speakers — there is simply nothing to select.

## Requirements

- Home Assistant **2026.8.0** or newer (Python 3.14)
- One or more Sendspin speakers on the same LAN — mDNS is link-local, so they
  must share a network segment
- [HACS](https://hacs.xyz/) for installation
- A Plum-Audio unit, *only* for the stream dropdown

## Installation

**Via HACS** (custom repository, until/unless this is listed by default):

1. HACS → ⋮ → **Custom repositories**
2. Add this repository's URL, category **Integration**
3. Download **Sendspin**, then restart Home Assistant
4. **Settings → Devices & Services → Add Integration → Sendspin**

Then adopt a speaker with **Add device** on the Sendspin entry. Discovered
speakers are pre-filled in the picker.

## Services

| Service | What it does |
|---|---|
| `sendspin.adopt_player` | Makes Home Assistant a speaker's Sendspin server. Takes the listener URL. |
| `sendspin.release_player` | Stops holding a speaker so another server can have it. |
| `sendspin.reclaim_player` | Takes a speaker back from whichever server currently holds it. |

Routing is not a service — it is `media_player.select_source` on the speaker.

## Documentation

- [Architecture](docs/ARCHITECTURE.md) — system design and key decisions
- [Open Questions](docs/OPEN-QUESTIONS.md) — unresolved blockers, and what the
  hardware actually did
- [Deployment & Test Loop](docs/DEPLOYMENT-TESTING.md) — how changes reach a live instance
- [Development Guide](docs/DEV-SETUP.md) — environment setup
- [CLAUDE.md](CLAUDE.md) — Claude Code project memory

## Tech Stack

- **Language**: Python 3.14
- **Platform**: Home Assistant custom integration (`custom_components/sendspin`)
- **Protocol**: [Sendspin](https://www.sendspin-audio.com/spec/) via
  [`aiosendspin`](https://github.com/Sendspin/aiosendspin) — **server** role,
  source-less. It renders no audio and never advertises itself over mDNS.
- **Distribution**: HACS

Not containerised — this integration has no audio pipeline of its own, so there
is nothing to ship in a container.

## Relationship to Music Assistant

[Music Assistant](https://www.music-assistant.io/player-support/sendspin/)
already supports Sendspin natively and does it well. This project deliberately
duplicates that surface for one reason: **removing the MA dependency.** If you
already run MA, you probably want MA. If you want Sendspin control without
adopting MA's whole ecosystem, you want this.

The two do not share nicely by design: a Sendspin speaker answers to one server
at a time, and adopting one here takes it away from Music Assistant. The
protocol offers no way to negotiate that, so this integration is deliberately
polite — it asks, and gives up if refused.

## License

[MIT](LICENSE)

---

**Repository**: <https://github.com/AnotherMike-exe/HA-Sendspin> ·
**Maintainer**: Plum Solutions
