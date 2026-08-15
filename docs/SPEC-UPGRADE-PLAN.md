# Encryption-Era Upgrade Plan

What has to change in this integration as the Sendspin peers on the network
move into the encrypted era, and — more of it than expected — what does not.

**Status: measured, and acted on. 2026-08-15.** Plum-Audio is on 9.1.x
(`phase3-dev`), Music Assistant is on the beta, and MA and the Plum endpoints are
paired to each other. The predictions written ahead of the upgrade were **partly
wrong in an important way** — see §0. Sections below are corrected in place, with
the superseded reasoning kept where it explains why the wrong answer looked right.

The work this analysis called for is tracked in
[ROUTING-RESTORATION-PLAN.md](ROUTING-RESTORATION-PLAN.md); its Track A shipped
as v0.3.7–v0.3.9 and routing now works across the fleet. **Pairing was not
needed for it** — see §5a, and note that §4b records the one identity change
this document originally missed.

Provenance is marked throughout, because the three grades are not
interchangeable:

- 📖 **read from source** — `aiosendspin` 9.1.0 as installed, cited `file:line`
- 🔬 **measured** — observed on this network, dated
- 🗣️ **reported** — stated by the Plum-Audio side or upstream docs, not yet
  verified here

---

## 0. What the 2026-08-15 pass measured

The fleet, as found. Nothing was playing anywhere; every source reported
`active: false` and both Plum players `playback_state: "stopped"`.

| Peer | Address | Identity |
|---|---|---|
| Plum Amp100 | `192.168.7.204` `:8927` server, `:8928` player | `UDtWfFDLwB…` / player `G2UChhEvyl…` |
| Plum RackPi | `192.168.7.122` `:8927` / `:8928` | `QraJSMSLgo…` / player `FjXD88ok30…` |
| Music Assistant | `192.168.7.226:8927` | `B3iRNtk3Bn-qzDVrf2-BhutRtpPQYyuVSamLSf4qZT4` |
| ESP32 / Voice PE | `esparagus-hifi-1`, `satellite1-d09ee8`, `home-assistant-voice-09472d` `:8928` | mDNS instance names, unchanged |

**Three findings, in order of how much they change the plan.**

1. 🔬 **The player rejects a second server outright. It no longer yields.** A dial
   from an unknown server into an upgraded Plum player is answered with
   `client/goodbye: concurrent_attempt` within ~5 ms, because Music Assistant
   already holds it. This **inverts Critical Rule 5**, which was written from the
   opposite observation — that a player always yields to the newest dialer. See
   §4.
2. 🔬 **The dial was never encrypted, and it still failed.** The Plum player
   accepted a cleartext `client/hello` from a stranger; it was arbitration, not
   Noise, that turned it away. So §2's "Noise mandatory on `:8928`" was wrong,
   and §3's predicted silent-inert adoption is not what happens. The real blocker
   is a rank problem, and pairing is the fix for a different reason than expected
   — a stronger one. See §3.
3. 🔬 **Cleartext controller reads are completely unaffected.** All three servers
   — both Plum units and MA's beta — admitted `legacy_client.py`'s exact
   pre-8.0 hello and activated `controller@v1`, `metadata@v1` and `artwork@v1`.
   `version: 1` is unchanged. No work here at all.

Two of the five open questions in §7 answered themselves off passive reads, and
both answered *well*: the mesh view's `player_id` matches the wire's `client_id`
exactly, so membership resolution is intact.

4. 🔬 **`ConnectionReason` is now decisive for every speaker on the network, and
   `async_adopt` uses the losing one.** A `DISCOVERY` dial is refused by all five
   speakers. A `PLAYBACK` dial is *admitted* by all three ESPHome speakers, with
   the full `player@v1` role set, held against Music Assistant for as long as we
   kept it. This is the single most actionable finding in the document — see §4a.

**Net effect on the plan.** Phase 3 (arbitration) is promoted to **first** and is
no longer a nicety: with `DISCOVERY`, adoption currently fails against every
speaker on this network. Phase 1 (detect) follows. Phase 2 (pairing) is
**narrower than it first appears** and covers only the two Plum players — see
§5a before building any of it.

---

## 1. This is not a library upgrade

The integration already pins `aiosendspin==9.1.0`. No bump is pending, and the
work below is not driven by one. What changes is the **peers**.

The version numbers in circulation refer to three different things and are
routinely conflated:

| Number | What it counts |
|---|---|
| `version: 1` in `client/init` | The **spec's** core message format. Has never moved. `legacy_client.py`'s `PROTOCOL_VERSION = 1` is still correct and does not change. |
| `aiosendspin` 6/7/8/9 | The **library**. 7.0.0 introduced encryption, 8.0.0 added AES-GCM and the source role, 9.x refined pairing. |
| "Sendspin v7+" in conversation | Shorthand for *the library version that crosses into encryption*, i.e. ≥ 7.0.0. |

Music Assistant's `dev` branch pins `aiosendspin[server]==9.1.0`. 🗣️ MA 2.9.x
stable pins `6.0.5`. So the MA beta is a 6.0.5 → 9.1.0 jump that crosses the
encryption boundary in one step — which is what "gets Sendspin to v7+" means in
practice.

---

## 2. Compatibility matrix

Four peer classes. Exactly one of them breaks — but not for the reason predicted.

| Peer | Direction | Transport after upgrade | Path in this integration | Work |
|---|---|---|---|---|
| ESP32 speakers (`sendspin-cpp`) | HA's server dials them | cleartext — 🗣️ no Noise in any release | hosted server, `allow_unencrypted=True` | **none** |
| Plum-Audio server `:8927` | HA dials it as a controller | 🔬 cleartext, admitted | `legacy_client.py` | **none** |
| Music Assistant server `:8927` | HA dials it as a controller | 🔬 cleartext, admitted on the beta | `legacy_client.py` | doc note only |
| **Plum-Audio player `:8928`** | **HA's server dials it** | 🔬 cleartext *accepted*, then **rejected by arbitration** | `server_host.async_adopt` | **all of it** |

🔬 2026-08-15, against all three servers: a hello of exactly `legacy_client.py`'s
shape was admitted by Plum Amp100, Plum RackPi and MA's beta alike. Each replied
`server/hello` with `version: 1`, `connection_reason: discovery` and
`active_roles: ["controller@v1", "metadata@v1", "artwork@v1"]`, then pushed
`server/state` for both `controller` and `metadata`, a `stream/start` naming the
512×512 JPEG artwork channel, and `group/update`. The connection stayed open.
**Reading now-playing needs no work whatsoever.**

The one change visible here is cosmetic but worth knowing: MA's `server_id` is
now its 43-char public key, and it advertises **that same string as its mDNS
instance name** (`B3iRNtk3Bn-…._sendspin-server._tcp`), where it used to publish
a friendly name. Plum's units still advertise `unit-7204` / `unit-7122`. Anything
that displayed a raw mDNS instance name for a server would now show a key.

### Why the servers are safe and the player is not

`allow_unencrypted` gates exactly one thing: which first frame is accepted on an
inbound socket (`server/server.py:157,188,266-268`; dispatch at
`server/connection.py:817-847`). It is **per-connection, per-frame** — the same
server instance serves encrypted, paired clients and cleartext legacy ones
simultaneously, and `is_encrypted` is a per-connection property
(`server/connection.py:368-371`). Nothing about it degrades globally.

There is **no client-side equivalent.** `grep -rn "legacy\|unencrypted"` over
`client/` and `noise/` returns zero hits. `SendspinConnection._run_noise_handshake`
calls `run_handshake_client()` unconditionally on every path
(`client/connection.py:330-379`), which unconditionally sends `client/init` as
its first frame (`noise/driver.py:146-153`). `SendspinClient.__init__` has no
`allow_unencrypted` parameter and takes `pairing_store` as a **required** keyword
(`client/client.py:200-222`) — the client cannot be instantiated without the
Noise machinery, let alone made to skip it.

That asymmetry is real in the library, and it is why this section originally
concluded that `:8928` demanded Noise:

- A Sendspin **server** may accept cleartext. Plum's and MA's do, so our
  hand-written controller keeps reading them.
- A Sendspin **client** has no cleartext mode *in `aiosendspin`*.

🔬 **The second bullet does not hold for Plum's player, and the conclusion drawn
from it was wrong.** Dialing `ws://192.168.7.122:8928/sendspin` from a stock
`SendspinServer` produced no `client/init` and no Noise handshake — the player
sent a plain `client/hello` (`trust_level: none`, `client_id: null`,
`software_version: phase3-dev`), and our server accepted it down the transition
branch at `server/connection.py:843`.

Since `grep -n "legacy\|unencrypted"` over `client/listener.py` and
`client/connection.py` still returns nothing, that cleartext listener is
**Plum's own code, not the library's**. Two consequences:

- It is a property of *Plum's* player, not of a 9.1.0 player in general. A
  spec-compliant third-party player would still demand Noise, and §3's
  silent-inert analysis is the right model for one.
- It is theirs to remove, on the same flag as everything else in this section.
  Do not build on it.

`legacy_client.py` is therefore **not** retired by this upgrade, and Critical
Rule 3's instruction not to simplify it back to the library still stands. Its
justification is stronger than when it was written, not weaker.

### The two cleartext dependencies worth naming

Both hang off the same flag, and if `PLUM_ALLOW_UNENCRYPTED` ever flips they die
together:

1. **Now-playing, artwork, progress and transport** from Plum units.
2. **`ctrl:<unit>:<source>` targeting.** A free-form `client_id` is only
   possible on a cleartext hello. Under encryption `client_id` *is* the static
   public key (§4), so the string cannot be chosen — the controller would fall
   back to blind `switch` hunting on Plum, as it already does on MA.

🗣️ Plum documents the flag as permanent rather than transitional, contingent on
sendspin-cpp having no Noise, MA stable pinning 6.0.5, and Plum's own web GUI
and `@sendspin/sendspin-js 3.2.1` browser player both being cleartext. It is
tracked as one change gated on all of those, and is not scheduled.

Upstream's own comment is less reassuring — `server/server.py:170-175` calls
transition mode "non-spec", and the published spec states outright that
"encryption is mandatory for all connections established through the standard
discovery mechanisms", with no backwards-compatibility pathway defined. Treat
the cleartext path as durable-by-configuration, not durable-by-spec.

---

## 3. The failure mode

🔬 **The measured failure is not the predicted one.** Both are worth keeping: the
first is what actually happens on this network today, the second is what happens
to a spec-compliant player and is still unmeasured.

### 3a. What actually happens — rejected on rank, not on trust

Dialing an upgraded Plum player that Music Assistant holds, from a server it has
never met:

```
Connection established
Received client/hello: name='Plum RackPi' … unpaired_access=UnpairedAccess(enabled=False)
Received client/goodbye with reason: GoodbyeReason.CONCURRENT_ATTEMPT
WebSocket closed, close_code=1000
```

Elapsed: ~5 ms. `ClientConnectedEvent` and `ClientAddedEvent` both fire, then
`ClientDisconnectedEvent`. Nothing was displaced — MA still held both players
afterwards, and the rig never noticed.

The decision is `SendspinClient._should_admit_connection`
(`client/client.py:661-679`), ranking connections by declared activity:
management(3) > playback(2) > pairing(1) > none(0).

- MA holds the player at **rank 0** — 🔬 the mesh view reports its
  `activities: []`, because nothing is playing.
- Our dial is also **rank 0**, and this is the trap: on the cleartext transition
  path the legacy `server/hello` *replaces* `server/hello` **plus**
  `server/activate` (`server/connection.py:886-905`). No `server/activate` is
  ever sent, so no activity is ever declared, so the rank is 0 by construction.
- Equal rank 0 falls to the tiebreak at `client/client.py:674-678`: admit the
  incoming connection only if **it** is `last_playback_server_id` and the
  incumbent is not. MA is. We are not. Refused.

🔬 Repeating the dial with `connection_reason=PLAYBACK` changes nothing —
byte-identical rejection. The reason is not clamped (the clamp at
`server/connection.py:898-904` spares `DISCOVERY` and `PLAYBACK`); it simply has
nowhere to be expressed, because `_initial_activities`
(`server/connection.py:1142-1154`) is only ever read when building a
`server/activate` that this path does not send.

**So: a cleartext dial can never take a Plum 9.1 player away from Music
Assistant, for any connection reason.** Not once, not with retries. It is not
a race that can be won — rank 0 versus rank 0 with the tiebreak held by the
incumbent is a deterministic loss. This is the hard blocker on adoption, and
§5's pairing work is what removes it.

### 3b. What the prediction got right, and where it still applies

For a player without Plum's cleartext listener, the original analysis stands and
is still 📖. An unpaired Noise dial **is not refused**. It
completes on the Sentinel PSK — a published constant that authenticates nothing
(`noise/constants.py:11`, selected at `server/connection.py:858-880`) — and then
quietly does nothing:

- `_playback_capable` is `False` for a sentinel PSK unless *both* the client
  advertises `unpaired_access.enabled` **and** the server holds a
  `trusted_unpaired` record (`server/connection.py:1091-1100`).
- `_roles_to_activate` returns `[]` when not playback-capable
  (`server/connection.py:1125-1132`).
- So `server/activate` carries `active_roles: []`, `activities: []`
  (`server/connection.py:1442-1458`).

`ClientConnectedEvent` still fires. `player_role(client)` returns `None`. The
speaker adopts, reports available, offers no volume, and is inert.

**One correction to that chain.** The original text said no goodbye is ever sent.
That is true only for the exact empty-set case, and it is worth knowing where the
boundary is, because it is the difference between a diagnosable failure and an
invisible one. The client validates the `server/activate` it receives
(`_admissible`, `client/connection.py:196-206`, applied at `445-471`):

| Sentinel PSK connection declares | `unpaired_access` | Result |
|---|---|---|
| no activities, no roles | either | **admitted, inert, no goodbye** — the silent case |
| `{PLAYBACK}` | enabled | admitted and works |
| `{PLAYBACK}` | disabled | `GoodbyeReason.PAIRING_REQUIRED` |
| any roles, no activities | disabled | `GoodbyeReason.PAIRING_REQUIRED` |

So the silent outcome is not the library's default — it is what **our** dial
specifically produces, because `_initial_activities` yields `[]` unless
`_playback_capable` is true, and `_playback_capable`
(`server/connection.py:1091-1100`) is false for an unpaired sentinel. Declaring
playback would earn a clean, actionable `PAIRING_REQUIRED` that
`_NON_RETRYABLE_GOODBYES` already handles. Declaring nothing earns silence.
Critical Rule 5's politeness is precisely what converts the diagnosable failure
into the invisible one.

🔬 And `unpaired_access.enabled` is **`False`** on the Plum player as shipped, so
the middle row is unavailable to us — answering open question 3, and ruling out
the zero-UI `trust_unpaired()` shortcut in §5.

**No goodbye is sent**, so neither branch of `_yield_reason` fires: not
`_NON_RETRYABLE_GOODBYES` (which already lists `PAIRING_REQUIRED` and
`UNPAIRED`, and is correct — it just never gets the chance), and not the flap
counter, because nothing disconnects. Silently adopted and silently useless is
the default outcome, and it is indistinguishable from success everywhere the
integration currently looks.

### Only `source@v1` requires pairing — and that is a red herring

`role_requires_pairing()` is a set lookup over roles registered with
`requires_pairing=True` (`server/roles/registry.py:35-46`), and the only such
registration in the package is `source@v1`
(`server/roles/source/__init__.py:16-19`). `player@v1`, `controller@v1`,
`metadata@v1`, `artwork@v1`, `color@v1` and both visualizers are registered
plainly.

Reading that alone suggests pairing is optional for everything we care about. It
is not: the per-role gate never runs, because `_roles_to_activate` returns the
empty list one level above it. Per-role pairing requirements only matter *after*
you are playback-capable.

---

## 4. Consequences for decisions already made

### Critical Rule 5 is inverted, not merely weakened

The rule reads: *"a player always yields to the newest dialer regardless of
connection reason — observed live against Music Assistant, using the polite
`DISCOVERY` reason."* 🔬 **That is no longer true of an upgraded player, and the
opposite is now the default.** An idle player MA holds refuses every dial we can
make (§3a). The rule's *conclusion* — never auto-dial, always make adoption an
explicit warned action — survives intact and is if anything more clearly right.
Its *premise* is stale, and the sentence should be rewritten before anyone
reasons from it again.

Practically, the failure moves from "we might steal a speaker by accident" to
"we cannot take a speaker on purpose". Those want opposite defences.

Arbitration is by declared-activity rank — `management(3) > playback(2) >
pairing(1) > none(0)`, strictly-greater wins, equal rank favours the newest
dialer, and rank 0 vs rank 0 is broken by the player's stored
`last_playback_server_id` (`client/client.py:648-677`). 🔬 Confirmed live: rank 0
against a rank-0 incumbent that owns the tiebreak yields `CONCURRENT_ATTEMPT` in
5 ms.

Our declared activities come from `_initial_activities`
(`server/connection.py:1145-1155`): `PLAYBACK` requires `dialed_playback or
_client_in_playback` **and** `_playback_capable`. Therefore:

| Dial | Rank | Outcome against an encrypted player |
|---|---|---|
| **cleartext, any reason** | **0** | 🔬 No `server/activate` is sent at all, so no activity can be declared. Deterministic loss. |
| `DISCOVERY`, unpaired | 0 | Loses the tiebreak to whichever server the speaker last played from. |
| `PLAYBACK`, unpaired | 0 | `_playback_capable` is False, so the activity is dropped. Same as above. |
| `DISCOVERY`, paired | 0 | Paired but declaring nothing. Same as above. |
| `PLAYBACK`, paired | 2 | Strictly outranks an idle incumbent. **The only row that holds the speaker.** |

That last row is the whole reason pairing became mandatory. It is not an
authorization requirement — it is the only way to reach a rank above 0, because
`_playback_capable` gates the `PLAYBACK` activity on a `LONG_TERM` PSK
(`server/connection.py:1096-1099`), and only pairing produces one.

**Both conditions are required.** `async_adopt` currently dials with
`ConnectionReason.DISCOVERY` deliberately — `server_host.py:214-219`, and the
reasoning in `OPEN-QUESTIONS.md` §1 — precisely to avoid asserting a claim Home
Assistant has no audio to justify. Against an encrypted player that politeness
is not merely ineffective, it is the difference between holding the speaker and
not.

This needs an explicit decision, not a quiet code change. The shape that
preserves the intent is to keep `DISCOVERY` for cleartext peers, where it still
means something, and use `PLAYBACK` only where the alternative is not working at
all.

🔬 §7's "a 9.1.0 server can dial out and hold a player for 30s" was measured
against a **cleartext** client and does not transfer. Do not cite it as evidence
for the encrypted path.

### 4a. The whole fleet, by dial reason — 🔬 measured 2026-08-15

Custody is the **only vendor-neutral routing mechanism this integration has.**
Plum's HTTP API covers Plum; everything else — ESPHome speakers, Voice PE, any
third-party endpoint — is reachable only by HA's hosted server dialing it. So
what a dial does across the whole fleet is the question that matters.

Every speaker was dialed from a throwaway server, unpaired, cleartext:

| Speaker | Impl | `client_id` | `DISCOVERY` | `PLAYBACK` |
|---|---|---|---|---|
| `esparagus-hifi-1` | ESPHome 2026.7.4 | `08:B6:1F:B7:AF:5C` (MAC) | ❌ `ANOTHER_SERVER` | ✅ **held, full roles** |
| `satellite1-d09ee8` | ESPHome 2026.7.4 | `98:A3:16:D0:9E:E8` (MAC) | ❌ `ANOTHER_SERVER` | not tested |
| `home-assistant-voice-09472d` | ESPHome 2026.7.4 | `20:F8:3B:09:47:2D` (MAC) | ❌ `ANOTHER_SERVER` | not tested |
| `player-7204` (Plum Amp100) | aiosendspin 9.1 + Plum listener | 43-char key | ❌ `CONCURRENT_ATTEMPT` | not tested |
| `player-7122` (Plum RackPi) | aiosendspin 9.1 + Plum listener | 43-char key | ❌ `CONCURRENT_ATTEMPT` | ❌ `CONCURRENT_ATTEMPT` |

Two different refusals, from two different implementations, for two different
reasons — and one of them is fixable for free:

- **ESPHome speakers honour the connection reason.** `DISCOVERY` is read as "I am
  only looking" and refused with `ANOTHER_SERVER`; `PLAYBACK` is read as a claim
  and admitted. The `PLAYBACK` dial came back with `active_roles = (PlayerV1Role,
  ControllerV1Role, MetadataV1Role)` and stayed connected for the full hold,
  against an MA that was holding it moments earlier. MA re-took it cleanly on
  disconnect; the HA entity never went unavailable.
- **Plum players ignore the connection reason**, because the legacy path declares
  no activities at all (§3a). Both reasons lose identically. Pairing is the only
  lever.

**`async_adopt` dials `DISCOVERY` (`server_host.py:214-219`). So on this network,
normal adoption currently fails against every speaker.** The only path that works
today is `sendspin.reclaim_player`, which reaches
`reclaim_client_for_playback()` → `connect_to_client(..., PLAYBACK)`
(`server/server.py:673-693`) — and it works for the ESPs precisely because it
uses the other reason.

This is Critical Rule 5 turned inside out. The rule assumed a `PLAYBACK` dial
would *rudely* take a speaker that `DISCOVERY` would politely leave alone. 🔬 The
opposite is true now: `DISCOVERY` takes nothing, from anything. The politeness
has no remaining effect except to make adoption fail.

### Critical Rule 1 (key on the frozen URL) is reinforced, but `client_id` changes shape

Under encryption `client_id` is the client's static X25519 public key —
43 characters of unpadded base64url, enforced by length and decode
(`noise/driver.py:394-407`, `noise/keys.py:18-22,86-89`). It moved out of
`client/hello` into `client/init` along with `version`, and the library's own
client never populates the hello fields at all
(`models/core.py:152-156`; `client/connection.py:977-992`).

The rule itself is unaffected and correct: the frozen listener URL stays the
identity. But two things downstream assume the *old* shape:

- `player_memo`'s stored `client_id` and `coordinator._client_id_for()` will see
  a pubkey where they used to see `player-7204` or a MAC.
- `mesh.player_by_id()` / `source_for_player()` match the wire's `client_id`
  against what Plum's `/api/mesh/view` reports.

🔬 **Open question 2 is answered, and the answer is good: the two agree exactly.**
Plum's mesh view reports `local_player.player_id` as the 43-char key, and the
same string arrives as the connection's `client_id` on the wire:

| Unit | `/api/mesh/view` `player_id` | wire `client_id` |
|---|---|---|
| Plum RackPi | `FjXD88ok30V-65lmRsztHRqAZ4mc2VGyqNjw_L0gxRM` | identical |
| Plum Amp100 | `G2UChhEvyl4jgdO6JlbLWCz2jnAxvm-Jh6TWpOSPgmk` | (not dialed) |

Membership resolution is therefore intact and `mesh.py` needs no change. Note
that the mesh view kept the old value too, in a **new** `listener_id` field
(`player-7204`), so both shapes are available if one is ever needed. `parse_view`
reads `player_id` (`mesh.py:239,260`), which is the correct one.

One subtlety worth recording: the cleartext hello carried `client_id: null`, yet
the connection still resolved the right 43-char id — so it is not being taken
from the hello payload. Where a legacy connection gets an authenticated-looking
id from is unresolved, and matters because `_admit_legacy_client_id`
(`server/connection.py:1067-1086`) **refuses** an unencrypted hello claiming a
`client_id` we hold a pairing record for. That is downgrade protection working as
intended, but it means the cleartext fallback stops being available for exactly
those players we have paired with. Expect it; do not treat it as a regression.

Pairing records are keyed by `client_id` (`noise/trust_store.py:314`), which is
also why staging a PSK before a dial requires knowing the player's public key in
advance.

### 4b. A *server's* id changed shape too, and that is the one that bit

The section above records `client_id` becoming a public key. 🔬 The same is true
of `server_id`, and that turned out to be the more expensive half — it broke two
things in ways no test caught, both found only by running against the fleet.

A Plum unit used to report a unit-scoped `server_id` in `server/hello`, close
enough to its `unit_id` that code identified a unit by comparing the two. Since
9.1.x it reports its X25519 public key:

| | Before | 🔬 Now |
|---|---|---|
| mesh `unit_id` | `unit-7204` | `unit-7204` (unchanged) |
| `server/hello` `server_id` | unit-scoped | `UDtWfFDLwBRGSZtv38GsSB1Rh9Dnc7aWJN0b53m781w` |
| mDNS instance name (MA) | friendly name | its public key |

Two consequences, both shipped as bugs and both fixed in v0.3.8:

1. **A unit stopped being recognisable as a unit.** `_foreign_servers` excludes
   Plum units from the "hand this speaker to another server" list, first by
   host and then — for a unit discovered over IPv6 as well as IPv4 — by
   comparing the link's `server_id` to `unit_id`. That comparison now never
   matches, so every unit reachable at a second address reappeared in the source
   dropdown as a bare server beside its own streams. The mesh view **already
   publishes** the unit's real `server_id`; `parse_view` simply never read it.
2. **Two links to one unit became indistinguishable.** A source link is keyed
   `<unit>:<source>` and a server link `server:<host>`, and they now report the
   same identity — so the duplicate-link sweep closed the source link every
   poll, the next sync rebuilt it, and a routed speaker's now-playing
   alternated between its track and nothing on a ten-second cycle.

**The lesson worth carrying into Track B and C**: this document tracked the
identity change carefully for *players*, where it turned out to be harmless
because the mesh view and the wire agreed. It did not think about *servers*,
where the same change quietly invalidated every equality test between an id
from the mesh and an id from the wire. Before touching pairing, grep for
comparisons between the two — they are the shape of this defect.

### `CONCURRENT_ATTEMPT` is unhandled, and it is now the reason we actually get

🔬 `_NON_RETRYABLE_GOODBYES` (`server_host.py:80-86`) lists `ANOTHER_SERVER`,
`UNAUTHORIZED`, `PAIRING_REQUIRED` and `UNPAIRED`. It does **not** list
`CONCURRENT_ATTEMPT`, which is the only goodbye an upgraded player actually sends
us. So `_yield_reason` falls through to the flap counter and we keep dialing —
with `retry_indefinitely=True` (`server_host.py:214-219`) — against a player that
has already given a definitive answer.

It does stop eventually: three disconnects inside `_FLAP_WINDOW_S = 120` trips
`YIELD_CONTESTED`. But that is the safety net doing the work of the precise
signal, it takes three rounds of hammering a speaker MA is using, and it reports
"contested" where the player said something exact.

Upstream disagrees with itself here, which is worth knowing before choosing a
fix: `should_retry_server_initiated_connection`
(`server/connection.py:341-356`) explicitly lists `CONCURRENT_ATTEMPT` as
retryable — "may retry later". That is defensible for a server polling for a
speaker to free up, and wrong for us, because our retry is indefinite and our
rank can never improve without pairing. **Adding `CONCURRENT_ATTEMPT` to
`_NON_RETRYABLE_GOODBYES` is a one-line change and should ship on its own**; it
converts three rounds of futile dialing into one clean "another server has this,
use reclaim".

### `ANOTHER_SERVER` retains client objects, in our configuration

On receipt, the dial task exits and does not retry
(`server/connection.py:342-356` — only `restart` and `concurrent_attempt`
retry), which is what `ServerHost` wants. But the client *object* is
deliberately retained rather than cleaned up: `_schedule_cleanup` short-circuits
and sets `_cleanup_on_mdns_removal = True` (`server/client.py:695-701`).

That hook only fires from the library's own mDNS browser
(`server/server.py:1171`), which this integration never starts — Critical Rule
3, and `server_host.py`'s "silent on mDNS" contract. So the flag is set and
nothing ever reads it. Retained client objects accumulate for the life of the
process. Not urgent, not visible, and trivially fixed with an explicit
`remove_client()` on yield.

🔬 **Bounded after all, for the reason we now hit.** A `CONCURRENT_ATTEMPT`
disconnect logs `Scheduling delayed cleanup in 180s`, so that path does reclaim
itself. The unbounded case is specific to `ANOTHER_SERVER` taking the
short-circuit; "forever" overstated it, and the leak is smaller than written.

The same event also ungroups the client asynchronously
(`server/client.py:653-654,669-683`), so a stolen speaker silently leaves its
group.

---

## 5. Pairing — required for custody, not for control

### 5a. What is actually blocked

§3a establishes that no unpaired dial can hold a Plum player another server is
holding. It is tempting to read that as "routing needs pairing". **It does not**,
and the distinction decides how much of this section gets built.

There are two routing mechanisms, and only one of them touches pairing:

1. **Plum's HTTP API** — vendor-specific, covers Plum units and any speaker a
   Plum unit can dial. No pairing.
2. **HA custody** — vendor-neutral, the only path to an ESPHome speaker, a Voice
   PE, or any third-party endpoint. Gated by arbitration, and 🔬 §4a shows the
   gate opens for ESPs on a `PLAYBACK` dial and stays shut for Plum players.

Mechanism 1 does not go over the Sendspin wire at all. `async_select_source`
(`media_player.py:193-262`) releases HA's own dial and then POSTs to the unit
that owns the source (`mesh.py:316-330`); the **Plum unit** dials the speaker,
over a pairing Plum and the player already have. HA's hosted server is not a
party to it.

| Capability | Path | Needs HA↔player pairing? |
|---|---|---|
| Source dropdown | `GET /api/mesh/view` | no |
| Route a speaker onto a stream | `POST /api/mesh/adopt` | no |
| Take a speaker off / hand to another server | `POST /api/mesh/release`, HA stops dialing | no |
| Volume & mute for a speaker a unit holds | `POST /api/mesh/volume` | no |
| Now-playing, artwork, transport | `legacy_client.py` → `:8927` | no — 🔬 measured working |
| Anything involving an ESP speaker | cleartext dial | no |
| **HA itself holding a Plum player** | hosted server dials `:8928` | **yes** |

Only the last row needs pairing — and 🔬 only for the **two Plum players**. The
same row for an ESPHome speaker is satisfied by a `PLAYBACK` dial with no pairing
at all (§4a). Custody covers parking a speaker on `Source: None`, the
adopt/reclaim services, HA-side volume for a speaker HA holds, and availability
driven by HA's own connection rather than the mesh's view.

**The fleet-wide answer**, since routability across *all* endpoints is the actual
requirement:

| Endpoint class | Routable & controllable today? | Via |
|---|---|---|
| ESPHome speakers, Voice PE | ✅ yes, once the dial reason is fixed | HA custody on a `PLAYBACK` dial |
| Any third-party cleartext player | ✅ likely, same mechanism | HA custody |
| Plum players | ⚠️ routing/volume/metadata yes; **custody no** | Plum HTTP API; custody needs pairing |
| Any third-party *encrypted* player | ❌ no | needs pairing |

So pairing is not what unlocks the fleet — the dial reason is. Pairing unlocks
the encrypted tail of it, which today is two speakers and tomorrow is however
many endpoints follow Plum into 9.x.

Two structural facts keep the rest working even when that dial fails:

- **The entity does not depend on the dial.** `async_adopt`
  (`server_host.py:193-219`) is fire-and-forget — `connect_to_client` is not
  awaited and the subentry is created regardless. A Plum player whose dial is
  refused still gets a `media_player`, and its `client_id` and dial URL come from
  the mesh view, not from a handshake.
- **Availability tolerates not holding it** (`entity.py:39-62`) — a yielded
  speaker the mesh can still see stays available, deliberately.

📖 **The one inference not yet measured**: `_should_admit_connection` returns
`True` unconditionally when no connection is currently admitted
(`client/client.py:664`). So a dial at an *unheld* player should succeed even
cleartext — which is why `Source: None` releases the unit's hold first and only
then dials. The measured rejection happened specifically because MA held the
player throughout. Worth confirming, because if it holds, even custody works
unpaired whenever the speaker is genuinely free, and pairing buys only the
contested case.

**Recommendation given the stated goal — "a simple way to control routing and
control": do not build pairing yet.** Ship Phase 1, confirm the inference above,
and see what is actually missing. Pairing's real value is winning a contested
player deterministically, which is a narrower want than it first looked.

### 5b. What it costs, if it is built anyway

Three methods exist (`noise/pairing.py`). In every one **the server initiates**,
by sending `server/activate` with `activities: ["pairing"]`
(`server/connection.py:1296-1307`); the client only reacts.

🔬 The Plum player advertises exactly two of them in its `client/hello`:

```
supported_pair_methods=[
  PairMethodDescriptor(method=PAIRING_PSK,  locations=['device', 'operator']),
  PairMethodDescriptor(method=DYNAMIC_PIN,  out_channels=['display'], min_pin_length=6),
]
```

**Static PIN is not offered**, so the worst-case row below is off the table
entirely. `PAIRING_PSK` listing `locations: ['device', 'operator']` is the
answer to open question 1: the token may be minted at either end, so HA can
generate one and have it entered in Plum's GUI, which is the direction that needs
no token-parsing UI at all.

| Method | User action | Gesture gate | Timing budget |
|---|---|---|---|
| **Pairing PSK** 🔬 offered | Paste one `SP:0…` token. No PIN, no PAKE round. | none | `SERVER_FIRST_MESSAGE_TIMEOUT_S = 60` |
| **Dynamic PIN** 🔬 offered | Speaker derives and emits a PIN; user reads it off the speaker and types it into HA. | only if escalated (≥10 failures) or PIN < 6 digits | `SERVER_ATTEMPT_TIMEOUT_S = 180` covers the typing |
| **Static PIN** 🔬 *not offered by Plum* | User types the device's fixed 8 digits. | **always** — 360s gesture window | 180s attempt |

The PIN methods need an async `pin_provider` hook wired to a config-flow future,
a countdown surfaced in the UI, and handling for six distinct
`PairAbortReason` values. The PSK token needs one text field.

**Recommended shape, given the stated goal of "a simple way to control routing
and control": implement the pairing-PSK token only.** `decode_token()` yields
both the `client_id` and the 32-byte PSK (`noise/pairing_token.py:46-64`), which
is exactly the pair needed to stage before dialing.

Two constraints from the Plum side, 🗣️ reported as having cost a working rig:

1. **Never attach a `pairing_attempt` to a dial that might land cleartext.** The
   library aborts the handshake with "pairing requires an encrypted connection"
   (`server/connection.py:842-845`). So the pairing path must be conditional on
   the peer being encrypted — which, for an ESP, it never is.
2. **Stage the PSK before the dial; do not pair an already-connected client.**
   `initiate_pairing()` on a live connection forces a mid-connection
   re-handshake (`server/connection.py:1400-1442`) that loses the race against a
   contended player.

### The zero-UI alternative

`server.trust_unpaired(client_id)` (`server/server.py:597-612`) persists a
`TrustedUnpairedClient` and re-activates the connection in place. No token, no
PIN, no exchange — a checkbox.

It requires the *player* to advertise `unpaired_access.enabled` in its
`client/hello`; both sides must agree (`server/connection.py:1091-1100`). Client
default for that flag is **False** (`noise/trust_store.py:191`).

🔬 **Ruled out.** The Plum player's hello carries
`unpaired_access=UnpairedAccess(enabled=False)`. Open question 3 is answered and
the shortcut is unavailable unless Plum changes a default on their side. Real
pairing it is.

Note the posture it would have had: the full negotiated role set under the
playback activity, authenticated by a published constant. Not weaker than the
cleartext path the ESPs already use, but not better either — so it is no great
loss.

### What must persist, and what breaks if it does not

Already handled, and correctly — `identity.py` persists the X25519 private key
via HA's `Store` and the pairing records via upstream's `FileServerPairingStore`.
Recording *why* it matters, because the failure is silent and total:

- The server has no id of its own — `server.id` **is** `identity.peer_id`
  (`server/server.py:184,251-253`). Rotating it makes every paired speaker
  refuse the handshake outright, because the client binds its stored PSK to a
  `server_id` and checks it post-match (`noise/driver.py:309-319`): *"PSK bound
  to server_id X, but connected to Y"*. Re-pairing is then required **even with
  the pairing store fully intact**.
- Losing the store alone is equally fatal and worse-behaved: the server falls
  back to the Sentinel PSK, the speaker still holds its half of the record, and
  the two are stuck in a mismatch that never resolves itself.
- **Downgrade protection is unconditional.** An unencrypted hello claiming a
  `client_id` the server holds any record for is rejected even with
  `allow_unencrypted=True` (`server/connection.py:1067-1089`). Once a device
  pairs with us it can never speak cleartext to us again — relevant only if a
  firmware rollback is possible.
- `server.unpair()` raises `ValueError` if the client is not connected
  (`server/server.py:588-596`). Deleting a subentry for an offline speaker
  leaves a stale record on the device.

---

## 6. Plan

🔬 **Reordered after the 2026-08-15 pass.** Arbitration moved from last to first:
it was written as a refinement, and it turns out to be the thing standing between
this integration and a working fleet. Pairing moved from second to last, because
it covers two speakers rather than all of them.

### Phase 1 — the dial reason (was Phase 3)

The whole fleet turns on this. 🔬 `async_adopt` dials `DISCOVERY`, which every
speaker on the network refuses; a `PLAYBACK` dial holds all three ESPHome
speakers with full roles (§4a). Until this changes, adoption does not work at all.

- Dial `ConnectionReason.PLAYBACK` from `async_adopt`. The politeness `DISCOVERY`
  was chosen for no longer exists — it does not yield a gentler outcome, it
  yields no outcome.
- Keep the explicit-consent posture, which was always the *real* protection and
  is untouched: nothing is auto-dialed, adoption stays a warned user action, and
  `ANOTHER_SERVER` still ends the dial.
- Rewrite Critical Rule 5 in `CLAUDE.md`. Its conclusion stands; both its premise
  and its stated mechanism are now false.
- Consider whether `reclaim` remains a distinct service once adopt uses the same
  reason. It may collapse into "adopt, and clear any yield" — which is nearly all
  it does today (`server_host.py:229-240`).

**This needs sign-off before implementation** — it amends a Critical Rule, and it
changes HA from a server that never takes a speaker into one that does. That is a
behaviour change users will notice, and it is the change that makes the feature
work.

### Phase 0 — record, build nothing

- Compat matrix (§2) into `ARCHITECTURE.md` §5.
- README warning: Music Assistant's **Allow legacy clients** toggle must stay
  on. It drives both `allow_unencrypted` and `allow_noncompliant_clients`,
  defaults to `True`, and is one click from killing all now-playing from MA.
- Record the two cleartext dependencies (§2) as a single linked risk.

### Phase 2 — detect, and stop the futile dialing (was Phase 1)

Ship independently of any pairing decision. Cheap, and it converts both the
silent failure in §3b and the three-round hammering in §4 into one clear answer.

- **Add `CONCURRENT_ATTEMPT` to `_NON_RETRYABLE_GOODBYES`.** 🔬 One line, no
  design questions, and it is now the reason we actually receive. Ship it first
  and separately. Message should name the holding server, which the mesh view
  already gives us (`local_player.server_name`).
- Treat *connected with zero active roles* as unavailable-pending-pairing, as a
  new yield reason alongside `contested`. Still the only signal a spec-compliant
  unpaired encrypted dial produces (§3b).
- Surface trust level, `active_roles` and `unpaired_access` per client in
  `diagnostics.py`. 🔬 All three were the deciding facts in this pass and none of
  them are visible today without attaching a debugger.
- Call `remove_client()` explicitly on yield, closing the retained-object leak
  in §4.

### Phase 3 — pairing, minimum viable (was Phase 2)

🔬 Unblocked (open questions 1 and 3 are answered) but **last**, because §4a shows
the dial reason unlocks the ESPs and Plum's HTTP API covers Plum's own routing.
What is left for pairing is exactly: **HA taking custody of an encrypted player**
— two speakers today.

Build it when that case is wanted, or when a third encrypted endpoint appears —
whichever comes first. The second is the reason not to defer it indefinitely: the
encrypted share of the fleet only grows.

- Pairing-PSK token step in `PlayerSubentryFlow`. 🔬 `PAIRING_PSK` is offered with
  `locations: ['device', 'operator']`, so **HA mints the token and the user
  enters it in Plum's GUI** — one generated string to display and copy, no
  parsing, no PIN, no countdown.
- `stage_pairing_psk()` before the first `connect_to_client` — never on a live
  connection (§5).
- Pairing path gated on the peer being encrypted, so ESPs are never offered it
  and never receive a `pairing_attempt`.
- Expect the cleartext path to that player to stop working afterwards, by design
  (downgrade protection, §4). Not a regression.

Note that for an encrypted player the two phases compose and neither is
sufficient alone: `PLAYBACK` **and** a long-term pairing record are jointly
necessary to reach rank 2, and rank 2 is the only state in which we hold the
speaker (§4).

---

## 7. Open questions — three now answered

Three of the five are 🔬 answered as of 2026-08-15.

1. ✅ **Which direction does the pairing token flow on Plum?** 🔬 Either — the
   player advertises `PAIRING_PSK` with `locations: ['device', 'operator']`. Take
   the operator direction: HA mints, the user pastes into Plum's GUI.
2. ✅ **Does `/api/mesh/view` report the new 43-char `client_id`?** 🔬 Yes, in
   `local_player.player_id`, and it matches the wire exactly. The old value moved
   to a new `listener_id` field. `mesh.py` needs no change.
3. ✅ **Is `unpaired_access` enabled on the Plum player?** 🔬 No —
   `UnpairedAccess(enabled=False)`. `trust_unpaired` cannot replace Phase 2.
4. **Does MA's beta leave *Allow legacy clients* on by default in practice?**
   🔬 Partly answered: MA's beta **does** admit our cleartext controller hello
   today, so it is on right now. What has *not* been done is deliberately turning
   it off to record exactly what is lost — that needs a hand on MA's settings.
5. **What does `allow_noncompliant_clients` tightening do to us?** Carried over
   from `OPEN-QUESTIONS.md` §7 and still unanswered. 🔬 The ESP fleet emits three
   distinct `client/state` violations and works only because the default is
   `True`. Separately, `start_stream` replacement behaviour *branches* on this
   flag (`server/group.py:129-138`) — a detail that will matter if this
   integration ever owns a live stream.

---

## 8. Verification checklist for the post-upgrade pass

Run against the upgraded fleet before changing code. Each turns a 📖 or 🗣️ into
a 🔬. Five done on 2026-08-15; three remain, and each needs something this pass
could not do on its own.

- [x] Dial an upgraded Plum player from the hosted server, unpaired. **Result:
      refused, not admitted** — `CONCURRENT_ATTEMPT` in ~5 ms, over a *cleartext*
      connection. The prediction was wrong on both counts; §3 rewritten.
- [x] Confirm `legacy_client.py` still reads now-playing from an upgraded Plum
      server. **Both units admit it and push metadata and artwork.**
- [x] Confirm the same against MA's beta with *Allow legacy clients* on.
      **Admitted, all three roles active.**
- [x] Capture the player's `client_id` from the wire and from `/api/mesh/view`
      side by side. **Identical.**
- [x] Capture the player's `client/hello` and check `unpaired_access.enabled`.
      **False.**
- [x] Confirm an ESP still accepts a cleartext dial. **All three ESPHome
      speakers do** — `client_id` is still a MAC, `version: 1`, full player role
      set on a `PLAYBACK` dial (§4a). A `DISCOVERY` dial is refused.
- [ ] Confirm an ESP adopts *through the integration* while an encrypted
      connection is live on the same server instance (§2, per-connection
      transition mode). *Not done:* HA has debug logging off and it does not
      survive a restart, so there was nothing in the journal. Needs
      `ha_probe.sh cycle`, which restarts the live instance.
- [ ] Confirm the `PLAYBACK` result on the other two ESPHome speakers. Only
      `esparagus-hifi-1` was tested for it; the other two were tested on
      `DISCOVERY` only, and there is no reason to expect a difference.
- [x] Confirm `ctrl:<unit>:<source>` targeting still places the controller link
      in the right group. **It does.** A link with a `ctrl:<source>:<nonce>` id
      lands in the named source's group and receives title, artist and a 65 KB
      artwork frame. Note the *ordering*, which cost a release: metadata arrives
      **before** the placement moves complete and is never repeated, so a client
      that treats a group change as a new context discards the only metadata it
      will get (fixed in v0.3.9).
- [ ] Turn MA's *Allow legacy clients* **off** and record exactly what is lost.
      *Not done:* deliberately — it is a destructive change to a working rig and
      wants a hand on MA's settings and a moment when nothing is playing.
- [ ] Restart HA with the identity store intact and confirm paired speakers
      reconnect without re-pairing. *Not done:* nothing is paired with HA yet.
      This becomes testable after Phase 2.
- [ ] **Dial a Plum player that nothing currently holds** and confirm it is
      admitted cleartext (§5a). *Not done:* MA held both players throughout.
      This is now the highest-value single check — it decides whether pairing is
      needed for custody at all, or only for taking a contested speaker.

Reproduction scripts for the completed items are in
`_resources/Research/spec-upgrade-probes/` — gitignored, as `_resources/` always
is. `probe_legacy.py` opens a read-only controller link to each `:8927` server;
`probe_dial.py` dials one player from a throwaway `SendspinServer` whose identity
is generated into a temp dir, so HA's own key and pairing store are never
touched. Neither sends a command, and the dial probe displaced nothing.

---

## 9. Sources

- Sendspin spec — <https://www.sendspin-audio.com/spec/>
- `aiosendspin` releases — <https://github.com/Sendspin/aiosendspin/releases>
- Music Assistant Sendspin provider — <https://github.com/music-assistant/server>
  (`music_assistant/providers/sendspin/`)
- Library citations throughout are `aiosendspin` 9.1.0 as installed in `.venv`.
- 🔬 measurements dated 2026-08-15, taken from this workstation against
  Plum-Audio `phase3-dev` on 9.1.x and Music Assistant's beta, with the two
  paired to each other and nothing playing. Probes in
  `_resources/Research/spec-upgrade-probes/`.
