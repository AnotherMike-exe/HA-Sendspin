# HA-Sendspin

> A Home Assistant custom integration for discovering and controlling Sendspin
> speakers on your LAN — **without requiring Music Assistant**.

Home Assistant becomes a Sendspin server of its own, so it can adopt speakers,
control them, and move them between the streams already running on your
network. If you want HA-native Sendspin routing today, the alternative is
installing all of Music Assistant to get the Sendspin slice. This is that
slice, on its own.

> **Status: v0.3.x — working, and honest about its edges.** Discovery,
> adoption, stream selection, now-playing, cover art and transport all work
> against real hardware. See [what doesn't work yet](#what-doesnt-work-yet).

---

## What works

- 🔍 **Automatic discovery** of Sendspin servers and speakers via mDNS. Nothing
  is ever adopted automatically — taking a speaker means taking it *from*
  whatever currently has it, so that is always your decision.
- 🔊 **One `media_player` per physical speaker**, durable across reconnects and
  IP changes. Volume and mute where the speaker reports them.
- 🔀 **Streams as a source dropdown.** Streams that are actually playing populate
  each speaker's source list; picking one hands the speaker to the unit running
  that stream. Several speakers on the same source *is* a group — that is
  Sendspin's own model, so there is no separate grouping control. A unit
  advertises every input it has configured, so inputs with nothing connected to
  them are filtered out: the dropdown holds what you can really route to, which
  is sometimes just "None".
- 📶 **Presence as availability**, so automations can trigger on a speaker
  dropping off the mesh with no polling script.
- 🎵 **Now-playing, cover art and transport** — title, artist, album, a
  progress bar and play/pause/next/previous, for a speaker **whoever is driving
  it**, including Music Assistant.
- 🔁 **Both directions.** Other Sendspin servers appear in the same dropdown as
  your streams, so a speaker can be handed to Music Assistant and taken back.
  Only servers you cannot already reach by naming a stream are listed.
- 🤝 **It declines to fight.** If another Sendspin server takes a speaker back,
  the integration stops dialling and tells you, rather than starting a
  tug-of-war that degrades both. `sendspin.reclaim_player` overrides that when
  you actually want the speaker.

## What doesn't work yet

- **No volume slider for a speaker another server holds.** Per-speaker volume
  can only be commanded by whatever holds the speaker's connection. Home
  Assistant can do it for speakers it holds, and Plum-Audio units expose it for
  theirs — but Music Assistant offers a controller only *group* volume, which
  would move every speaker on the stream at once. See
  [OPEN-QUESTIONS §8](docs/OPEN-QUESTIONS.md).
- **No seek.** There is no seek command at any Sendspin version, so the progress
  bar is read-only.
- **Home Assistant plays no audio of its own.** It routes and controls; it is
  not a source.
- **The stream list needs a Plum-Audio unit.** No Sendspin server exposes a way
  for an outsider to enumerate its streams, so the source dropdown comes from
  Plum-Audio's mesh API. Without one you still get speakers, other servers as
  sources, and now-playing — just no per-stream routing targets.
- **Handing a speaker to another server is a release, not a handoff.** The
  protocol has no "give this speaker to that server" verb, so choosing another
  server stops Home Assistant holding the speaker and lets that server's own
  dialling take it.

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
- [Encryption-Era Upgrade Plan](docs/SPEC-UPGRADE-PLAN.md) — what changes as the
  fleet moves to Noise, and what does not
- [Routing Restoration Plan](docs/ROUTING-RESTORATION-PLAN.md) — the work to get
  every endpoint class routable and controllable again
- [Deployment & Test Loop](docs/DEPLOYMENT-TESTING.md) — how changes reach a live instance
- [Development Guide](docs/DEV-SETUP.md) — environment setup
- [CLAUDE.md](CLAUDE.md) — Claude Code project memory

## Tech Stack

- **Language**: Python 3.14
- **Platform**: Home Assistant custom integration (`custom_components/sendspin`)
- **Protocol**: [Sendspin](https://www.sendspin-audio.com/spec/) via
  [`aiosendspin`](https://github.com/Sendspin/aiosendspin) — **server** role,
  source-less. It renders no audio and never advertises itself over mDNS.
  The **controller** role is hand-written (`legacy_client.py`): the library's
  client speaks only the 8.0+ handshake, which no Sendspin server in the field
  accepts.
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
