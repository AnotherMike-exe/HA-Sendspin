# Open Questions

Design questions that change the *shape* of the integration, not code TODOs.
Started from the original project outline
(`_resources/Notes/sendspin-ha-integration-outline.md`).

Status legend: 🔴 blocking · 🟡 needs a design pass · 🟢 decided

**Read §7 first.** It is the only section describing what the hardware actually
did rather than what was intended, and most of the decisions above were settled
by it. §8 is the one live question.

---

## 1. Multi-server arbitration — 🟢 resolved as policy, not as protocol

**Question**: When a player could be claimed by more than one Sendspin server,
who wins?

The Sendspin spec does not yet define this (see `docs/UPSTREAM-AIOSENDSPIN.md` §1
in Plum-Audio). Any *generic* router hits this immediately. Plum-Audio sidesteps
it entirely by being the sole authority for its own mesh — this project cannot
make that assumption, because its whole premise is controlling arbitrary servers
on the LAN.

**Why it blocks**: `roam_player` has no defined correct behaviour until this is
settled. Shipping a service that races two servers is worse than not shipping it.

**Resolved by refusing to participate.** The spec gap is untouched — it is not
ours to close — but the integration no longer has undefined behaviour:

- Adoption dials with `ConnectionReason.DISCOVERY`, never `PLAYBACK`. Home
  Assistant has no audio to justify asserting a claim.
- A discovered speaker is **never** adopted automatically.
- A `GoodbyeReason.ANOTHER_SERVER` ends the dial and surfaces "another server
  holds this", instead of retrying into a tug-of-war.
- `sendspin.reclaim_player` is the explicit override, which the user asks for.

Confirmed live: politeness does **not** protect a contested speaker (§7). It
only stops us making the contest worse.

---

## 2. Entity identity — 🟢 decided

**Question**: Which Sendspin device name should HA display, and what do entities
key on?

Two sub-problems:

- **Two name views.** A Sendspin device presents a handshake name and an mDNS
  instance name, and they are not guaranteed to match. Picking the wrong one
  produces entity names that disagree with what the user sees elsewhere.
- **Key on the listener URL, not the client id.** A Sendspin client's id is *not
  stable across the two identity views*. Plum-Audio hit this directly — see the
  `speaker-identity-gui` note in project memory. Keying entities on the client id
  orphans them on rename or reconnect.

**Decided.** The unique id is the listener URL **as first seen, frozen** —
`player:{frozen_url}` — with the live dial URL kept separately so DHCP cannot
orphan an entity, and the mDNS instance name as a secondary matcher so a moved
speaker updates its address instead of acquiring a second entity.

The display-name question is closed too: **handshake name > `name` TXT > mDNS
instance name > `host:port`**, persisted in `player_memo.py` and **never
demoted** — a later mDNS-only sighting must not overwrite a handshake name,
because the good name stops being visible exactly when the speaker goes
offline. A later *handshake* may still update it, so a genuine rename lands.

---

## 3. Scope of "routing" — 🟢 resolved, and smaller than the question assumed

**Question**: Do intra-server re-route and cross-server roam share HA service
semantics?

They do not, and assuming they do is the trap:

| | Intra-server re-route | Cross-server roam |
|---|---|---|
| Mechanism | `group.add_client` / `remove_client` | reconnect-based |
| Liveness | live | player drops and re-attaches |
| Gotcha | group membership is fixed at `start_stream()` — **a player added to a live group joins silent** unless the stream is refreshed. Plum-Audio had to add this behaviour explicitly. | subject to §1 arbitration |

**Resolved: neither is a service.** Routing is `media_player.select_source` on
the speaker entity. Several speakers on one source *is* the group — Sendspin's
own semantics — so no grouping feature is advertised, and roam is not a distinct
operation but simply selecting a source that lives on another unit.

All three scaffolded services are retired. What remains is the adoption
lifecycle: `adopt_player`, `release_player`, `reclaim_player`.

The membership window (`stop_stream` → mutate → settle → `start_stream`) is
still required wherever *we* own a live stream — but a source-less server never
does, so it is unbuilt and unneeded for now.

---

## 4. Packaging — 🟢 decided (revisit only if forced)

**Decision**: custom integration distributed via HACS. Not a Supervisor add-on.

**Rationale**: no audio pipeline of its own, so nothing to containerise — and an
add-on cannot create `media_player` entities at all, so the add-on route would
require a companion integration regardless.

**What would force a revisit**: if discovery turns out to need host networking in
a way a plain integration cannot get. HA core's `zeroconf` integration should
cover this; if it does not, a companion add-on becomes the fallback rather than a
replacement.

---

## 5. Repo location, name, and license — 🟢 decided

<https://github.com/AnotherMike-exe/HA-Sendspin>, domain `sendspin`, MIT.
hassfest and the HACS action both pass.

---

## 6. Protocol drift ownership — accepted cost

Not a question so much as a standing liability recorded at project start: this is
a **second independent Sendspin controller-role implementation** with no upstream
to lean on. Music Assistant tracks spec drift for its implementation; this
project tracks its own. Accepted going in.

`docs/HARD-WON-LESSONS.md` in Plum-Audio catalogues the identity, membership, and
idle-state traps — this project will hit them again independently and does not
inherit the fixes.

---

## 7. Hardware findings — M1 gate, 2026-08-11

Run against the live LAN with a source-less `aiosendspin==9.1.0` server and
`allow_unencrypted=True`, dialling out from `ServerHost.async_adopt`.

### ✅ The architecture's core assumption holds

A 9.1.0 server **can dial out to a pre-8.0 cleartext client and complete a
handshake**. Only the *inbound* legacy branch had been confirmed by reading
source; the outbound path is the only one this integration uses, and it works:

```
WARNING Accepting unencrypted legacy connection (transition mode)
DEBUG   Received client/hello: name='Plum Amp100', client_id='player-7204', version=1
[event] ClientConnectedEvent(client_id='player-7204')
```

Adoption of `ws://192.168.7.204:8928/sendspin` held **continuously for 30s**,
negotiating `player@v1, controller@v1, metadata@v1, visualizer@v1, artwork@v1`.
Hosting a server is therefore available to the existing fleet.

### A first dial against a held player is refused — retry is mandatory

A single-shot dial (`retry_indefinitely=False`) completed the handshake and then
lost the socket 1ms later. A Sendspin player refuses a second socket, drops its
current server, and **expects the new one to retry**. With
`retry_initial_connection=True, retry_indefinitely=True` — what `async_adopt`
uses — the same dial succeeded and held. Do not "optimise" those flags away.

Note this is *not* caused by the artwork role's `stream/start` on join, which
upstream emits for every server regardless of whether a stream exists.

### 🔴 Contested devices flap, and Music Assistant wins

Dialling the Satellite1 (`ws://192.168.7.151:8928/sendspin`) completed the
handshake, learned the name, and was then taken straight back:

```
[event] ClientConnectedEvent(client_id='98:A3:16:D0:9E:E8')
[event] ClientDisconnectedEvent(client_id='98:A3:16:D0:9E:E8',
                                goodbye_reason=GoodbyeReason.ANOTHER_SERVER)
```

It never reconnected across 30s of retries: Music Assistant re-dials harder.
This is §1's arbitration gap, live — and it happened despite dialling with
`ConnectionReason.DISCOVERY` rather than `PLAYBACK`, so **politeness does not
protect a contested device**.

**Design consequence:** a `GoodbyeReason.ANOTHER_SERVER` goodbye must stop the
retry loop and surface "another server holds this" to the user, rather than
retrying forever and producing a tug-of-war that degrades both integrations.
Forcing the issue is `reclaim_player`, and the user has to ask for it.

The client id is a **MAC** (`98:A3:16:D0:9E:E8`) while mDNS names by instance —
the two-identity-view split, observed directly. The handshake name
(`FutureProofHomes - Satellite1`) *was* learned even though the connection did
not persist, so the name memo can be populated from a failed adoption.

### `client/state` non-compliance is real and tolerated only by default

```
non-compliant client: initial client/state omitted the required 'available' field
non-compliant client: client/state used legacy player.state instead of top-level available
non-compliant client: client/state used the legacy top-level 'state' field
```

Confirms `UPSTREAM-AIOSENDSPIN.md` §1. The fleet works only because
`allow_noncompliant_clients` defaults to `True`. **New open question:** what
happens when upstream tightens that default, alongside the existing question
about `allow_unencrypted` being removed from transition mode.

### 🟢 RESOLVED — the controller link cannot use the *library*, so it was written by hand

Probed with a controller-role `SendspinClient` from aiosendspin 9.1.0. All three
servers reject it identically:

```
HandshakeAbortedError: expected server/init (TEXT), got CLOSE
```

| Server | Result |
|---|---|
| Music Assistant, `192.168.7.226:8927` | ❌ |
| Plum Amp100, `192.168.7.204:8927` | ❌ |
| Plum RackPi, `192.168.7.122:8927` | ❌ |

Every one predates 8.0. A 9.1.0 client always initiates the Noise handshake and
has **no client-side `allow_unencrypted`** — that flag exists only on the
server. So the client cannot downgrade, and the connection dies before
`client/hello`.

**This is an asymmetry, not a version mistake.** The same 9.1.0 package is fine
as a *server*: `allow_unencrypted=True` accepts these very devices, proven on
hardware above. It is only the *client* half that cannot talk to them. One
installed version has to serve both roles, and none serves both against this
fleet:

| | server role | client role |
|---|---|---|
| **9.1.0** | ✅ accepts legacy cleartext | ❌ cannot dial legacy servers |
| **6.0.5** | ✅ natively cleartext | ✅ |

**What it blocks:** M5 only — metadata, artwork, progress and transport, which
all require a controller socket joined to a playing group. **M4 is unaffected**:
source listing and routing run over the mesh REST API on plain HTTP.

**Resolved by option 1**, in v0.2.0. `legacy_client.py` speaks the pre-8.0
protocol directly and is admitted by every server on the network. Verified live:
title, artist, album, progress, 25-29KB of JPEG cover art and the full transport
set, from both a Plum unit and Music Assistant.

Two behaviours had to be learned on hardware, and are recorded in
`docs/ARCHITECTURE.md` §3.5: Plum honours a `ctrl:<source_id>` client id and
places the controller directly, while Music Assistant does not and has to be
found with `switch`.

**Options as they stood:**

1. **Hand-roll a minimal legacy controller client.** The pre-8.0 wire protocol
   is plain JSON over a websocket — `client/hello` → `server/hello`, then
   `group/update`, `server/state`, `client/command`, `client/time`. Plum-Audio's
   `frontend/services/sendspinControllerClient.ts` is a working reference, and
   `docs/SENDSPIN-CONTROLLER-PROTOCOL.md` documents it. Decouples the controller
   role from the library version permanently, at the cost of owning a second
   protocol implementation — which is the drift liability §6 already accepts.
2. **Pin 6.0.5 for both roles.** Works against everything today. Forfeits
   forward compatibility with Noise-era clients, and means rewriting
   `identity.py` and the server construction, since 6.0.5 has no `Identity` and
   no pairing store.
3. **Defer metadata.** Ship M4 without it and revisit when the servers upgrade.

### Sources are routinely all-idle — and that is the signal, not a problem

Every source across `unit-7204` and `unit-7122` read `active=False,
streaming=False`, and the three bare players did not appear in any unit's
`players` list.

**The conclusion originally drawn from this was wrong and has been reversed.** It
read the all-idle mesh as proof that filtering would render an empty dropdown, so
`source_list` listed all six sources with active ones sorted first. But a Plum
unit publishes every *configured input* as a source whether or not a sender is
connected — the flags are how it says which of them are real. Listing them all
offered an AirPlay endpoint with nothing connected to it as though a speaker
could usefully be put there, and one such phantom kept serving the retained
metadata of a track that had long finished.

Re-measured 2026-08-12 with one AirPlay sender running into `unit-7122`:

```
unit-7122  airplay-1   'VLAN7 AirPlay'   active=True   streaming=True   srcvol=20
unit-7122  spotify-1   'VLan7 Spotify'   active=False  streaming=False
unit-7204  airplay-1   '204 AP'          active=False  streaming=False
    (…three more, all false)
```

The flags work, and exactly one source qualified. **`source_list` now filters on
`active or streaming`**, and an empty-but-for-`None` dropdown is the correct
answer when nothing is playing — which is also what the Plum GUI has always
shown.

Two details the filter depends on:

- **`active or streaming`, never `streaming` alone.** `streaming` drops on a
  pause, which would take a paused sender's source — and the user's selection —
  out of the dropdown mid-track. `active` is the tolerant flag, held true for up
  to `SOURCE_IDLE_TIMEOUT_S` (300s) after a sender walks away, and that tail is
  the residual lag on a dead source disappearing.
- **The current selection is pinned** into the list even once it stops being
  live. Otherwise a stream ending under a routed speaker leaves the entity
  reporting a source absent from its own options, which renders as blank.

A frozen mesh view keeps its identities but stops asserting liveness after
`_MESH_LIVENESS_TTL_S` (60s). An unreachable mesh must never read as every stream
having ended, but "something is feeding this" is a claim about *now*.

### `player_ids` is empty on real hardware, so membership comes from `group_id`

Related, and the reason the above went unnoticed: `source.player_ids` and
`unit.players` are `[]` on **every** unit of a live mesh. `source_for_player`
matched on `player_ids` alone, so it never matched, so every speaker read as
being on no stream — and an entity reported `source: 'Home'`, our own server's
name, a value present in no dropdown.

The signal that does exist is `local_player.group_id`, which equals the
`group_id` of whichever source the speaker is on. Confirmed against the wire: a
controller link to `unit-7122` reported group
`21f06312-6e14-4e5a-9d28-133d84ef126e` as `playing`, the same id the mesh gives
for that unit's `airplay-1`. Membership is now resolved by that match, keeping
the `player_ids` match as the cheaper answer wherever a unit populates it.

Because the group id moves whenever *anything* re-routes the speaker, this is
also how a change made from Music Assistant or the Plum GUI becomes visible here,
and how a return to none is detected rather than sticking.

**Confirmed with a speaker actually routed, 2026-08-12.** `player-7204` on
`unit-7204` was put on `unit-7122`'s AirPlay source:

```
unit-7122 airplay-1 'VLAN7 AirPlay' active=True streaming=True
                    group=8cdd79b4-… players=['player-7204']
unit-7204 local_player player-7204 group=8cdd79b4-… held='Plum RackPi'
```

Two things this settles:

- **A remote holder reports the holding server's group id**, not one of its own.
  This was the open risk — group matching would otherwise have worked only for
  locally-held speakers.
- **`player_ids` *does* populate once a speaker is assigned.** It reads `[]` only
  because an unrouted speaker is in no membership list. So both branches of the
  match agree, and keeping the direct one first costs nothing.

### The now-playing flap was three bugs, and a dead flag hid one for three releases

Reported as: select a source, get artwork and metadata, watch it fall to idle and
come back, repeatedly. Also a dead AirPlay source showing the cover of the last
thing Music Assistant played. One symptom, three causes, none of them a watchdog
or a timeout:

1. **`hunt_for_playing` was never read.** It was stored as `self._hunt` and
   `_find_playing_group` ran for every link regardless, so a `ctrl:<source>`
   link — which Plum has *already* placed on the group it was aimed at — fired
   `switch` every four seconds and was moved off it. That is the four-second
   cycle. `_SWITCH_REST_S` was likewise defined and never referenced.
2. **`supported_commands` was clobbered.** Read as `... or ()`, so an absent key
   became "no commands" where every neighbouring field treats absent as
   unchanged. Any `server/state` carrying only a volume change stripped the
   transport set, and Home Assistant said so out loud: *"Entity … is updating its
   capabilities too often."*
3. **Artwork outlived its stream.** Cover art is only cleared by an empty binary
   frame, deliberately, so a pause does not blank it — but nothing cleared it on
   a *group change*, so a moved link kept serving the old stream's art.

**The lesson worth keeping is about the tests, not the code.** All 142 tests
passed over (1) for three releases, because the two hunt tests asserted
`client._hunt is False` — the value of the flag, not whether any `switch` reached
the wire. A test that cannot fail when the behaviour is absent is not covering the
behaviour. Both now drive a real connection against a fake socket and assert on
what was sent.

Confirmed on hardware afterwards: a speaker routed to a live AirPlay source held
`state=playing`, one constant source, and `supported_features=18493` unchanged
across fourteen samples over three and a half minutes, with the `ctrl:` link
still on group `8cdd79b4-…` and zero capability warnings.

**Deliberately not fixed:** `_sole_playing_link` still declines when two servers
play different tracks, so a speaker attributed only by that rule loses its
metadata when an unrelated server starts playing. Making the choice sticky would
smooth it, at the cost of showing a known-wrong track on a speaker. Better no
track than the wrong one — and there is a test asserting exactly that.

### Handing a speaker to a server is a request, not a command — and it is slow

There is no Sendspin verb for "give this speaker to that server". Selecting a
server in the dropdown can only stop us holding the speaker and leave the target
to dial it, and a server dials a player when *it* decides to.

Measured 2026-08-12, handing `player-7204` to Music Assistant. For the **four
minutes** watched, the mesh reported:

```
local_player player-7204  attached=false   (no server_id, no server_name)
```

Held by nothing, silent, and indistinguishable from a hand-off that failed
outright. Checked again later: `attached=true, server_name='Music Assistant'`,
with no further action from anyone. It landed; it was just slow.

This produced two wrong fixes in a row before the right one, and the sequence is
worth keeping:

1. Never rescuing a server hand-off left a genuinely abandoned speaker orphaned.
2. Rescuing as soon as the mesh said "unheld" stole the speaker back at ~t+60s,
   before the server arrived — the tug-of-war §1 and rule 5 exist to prevent.

The resolution is a hand-off grace of its own, `_SERVER_HANDOFF_GRACE_S`, an
order of magnitude longer than the 45s a *stream* hand-off gets. Past it, with the
mesh still reporting nothing holding the speaker, it is taken back and a warning
names the server that did not take it. A hand-off predating a restart is left
alone: the timestamp is in memory only, so the window cannot be timed, and
guessing contests a server that may be holding the speaker happily.

**Consequence for the UI, unresolved:** for those minutes the speaker reports
`None` and offers no volume, because nothing holds it and that is the truth. A
user who has just chosen "Music Assistant" sees the selection apparently not
take. Showing the *intent* instead would be a lie whenever the hand-off never
lands. Worth revisiting if it proves confusing in practice.

### AirPlay carries no metadata into the Sendspin group

A speaker on a live AirPlay source showed transport and state but no title,
artist or cover art. The controller link is attached correctly — it is on
`unit-7122:airplay-1`, group `8cdd79b4-…`, `state=playing`, with the artwork role
negotiated and `stream/start` sent — and the metadata arriving on that group is
all-null. Nothing is being dropped on our side; there is nothing there.

Probed one hop further back: Music Assistant, which was feeding that AirPlay
input, reports all-null metadata on its *own* Sendspin server too, and its only
group reads `stopped`. It is not playing to a Sendspin group at all — the audio
path is MA → AirPlay sender → Plum's AirPlay receiver → Plum's Sendspin group.

Plum demonstrably *can* carry metadata on an AirPlay source: an earlier capture
of `unit-7204:airplay-1` served `title='Imaginary Friends', artist='deadmau5'`.
So the gap is one of the two hops, and which one is worth knowing:

- if a **direct** AirPlay sender (phone, Mac) into the same source produces
  metadata, then MA's AirPlay output is not sending DAAP metadata, and this is
  not fixable from either this repo or Plum;
- if it does not, Plum's AirPlay receiver is not mapping RAOP metadata onto the
  Sendspin metadata role, which is a Plum-side fix.


---

## 8. Per-speaker volume for a speaker another server holds — 🟡 needs a decision

**Question**: how should Home Assistant offer volume for a speaker it does not
hold?

Per-speaker volume can only be commanded over the connection the *holding*
server owns. Three cases:

| Who holds the speaker | Volume available? | How |
|---|---|---|
| Home Assistant | ✅ | `player@v1` role over our own connection |
| A Plum-Audio unit | ✅ | `POST /api/mesh/volume` on that unit |
| Anything else, e.g. Music Assistant | ❌ | no per-player control exists for us |

Confirmed on hardware: a speaker Music Assistant holds reports `volume: null`
and offers no slider, because nothing we can reach knows its level.

**The tempting option, and why it is not obviously right.** A controller link
does expose a `volume` command — but it is **group** volume for the stream, not
the speaker. With one speaker on a stream the two coincide; with three, moving
one entity's slider moves all three. Shipping that as a per-speaker control
would be quietly wrong in exactly the multi-room case this project exists for.

**Options:**

1. Offer it anyway, documented as stream volume. Simple; misleading when a
   stream has several speakers.
2. Expose stream volume as a separate control — the stream entity described in
   the original plan, which was deferred and never built.
3. Leave it absent, as now, and rely on the holding server's own UI.
