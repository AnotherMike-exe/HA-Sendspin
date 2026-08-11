# Open Questions

Unresolved design questions carried over from the original project outline
(`_resources/Notes/sendspin-ha-integration-outline.md`). These are **blockers on
design, not code TODOs** — each one changes the shape of the integration
depending on how it resolves. Resolve before the surface they touch is built.

Status legend: 🔴 blocking · 🟡 needs a design pass · 🟢 decided

---

## 1. Multi-server arbitration — 🔴 blocking

**Question**: When a player could be claimed by more than one Sendspin server,
who wins?

The Sendspin spec does not yet define this (see `docs/UPSTREAM-AIOSENDSPIN.md` §1
in Plum-Audio). Any *generic* router hits this immediately. Plum-Audio sidesteps
it entirely by being the sole authority for its own mesh — this project cannot
make that assumption, because its whole premise is controlling arbitrary servers
on the LAN.

**Why it blocks**: `roam_player` has no defined correct behaviour until this is
settled. Shipping a service that races two servers is worse than not shipping it.

**Options not yet evaluated**: last-writer-wins; integration-side lock keyed on
player; refuse to roam a player that another known server currently claims.

---

## 2. Entity identity — 🔴 blocking

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

**Decided so far**: listener URL is the unique id. This is already reflected in
`const.py` and `config_flow.py`. The *display name* question is still open.

---

## 3. Scope of "routing" — 🟡 needs a design pass

**Question**: Do intra-server re-route and cross-server roam share HA service
semantics?

They do not, and assuming they do is the trap:

| | Intra-server re-route | Cross-server roam |
|---|---|---|
| Mechanism | `group.add_client` / `remove_client` | reconnect-based |
| Liveness | live | player drops and re-attaches |
| Gotcha | group membership is fixed at `start_stream()` — **a player added to a live group joins silent** unless the stream is refreshed. Plum-Audio had to add this behaviour explicitly. | subject to §1 arbitration |

**Reflected in the scaffold**: `services.yaml` deliberately declares
`group_add_player` / `group_remove_player` separately from `roam_player`. Do not
collapse them.

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

## 5. Repo location, name, and license — 🔴 blocking release

Not yet decided:

- Whether this lives under the same GitHub org as Plum-Audio.
- Final repo name (scaffolded as `HA-Sendspin`, domain `sendspin`).
- License.

**Why it blocks**: `manifest.json` requires `documentation`, `issue_tracker`, and
`codeowners`, all currently `TODO-ORG` / `TODO-GITHUB-USERNAME` placeholders.
hassfest and the HACS action will fail until these are real URLs.

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

### 🔴 The controller link cannot reach any server on the network

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

**Options, none yet chosen:**

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

### Sources are routinely all-idle

Every source across `unit-7204` and `unit-7122` read `active=False,
streaming=False`, and the three bare players did not appear in any unit's
`players` list. Filtering `source_list` to active sources would therefore have
rendered an **empty dropdown**. Routing a speaker to an idle source is a normal
workflow — assign it, then start playing — so `source_list` shows all sources
with active ones sorted first.
