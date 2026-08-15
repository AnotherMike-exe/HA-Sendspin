# Routing Restoration Plan

Getting back to Sendspin-6-equivalent routing and control across **every**
endpoint class on the network — ESPHome speakers, Voice PE, Plum-Audio players
and any third-party endpoint — preferring a route that needs no pairing.

**Status: Track A shipped and verified on hardware, 2026-08-15 (v0.3.7 → v0.3.9).**
Track B is still undecided and waits on the G1/G2 gate tests in §5. Measurements
are from the pass recorded in [SPEC-UPGRADE-PLAN.md](SPEC-UPGRADE-PLAN.md); this
document is the *what to build* half and does not repeat the analysis.

### What shipped

| Release | Change |
|---|---|
| **v0.3.7** | A1–A5: adoption dials `PLAYBACK`, `CONCURRENT_ATTEMPT` is non-retryable, reclaim re-dials through adoption, diagnostics gained a `clients` block, yielded clients are released |
| **v0.3.8** | Two regressions the deploy exposed — see below |
| **v0.3.9** | A pinned `ctrl:` link keeps the metadata it was placed with |

🔬 **Verified live**: routing and source selection work for both Plum endpoints
and ESPs; now-playing is stable and correct on a routed speaker; the source
dropdown lists only real streams.

### Three faults the deploy found that the plan did not predict

All three were invisible to the test suite and only appeared against hardware.
Worth recording, because two of them share the cause the upgrade analysis
already identified and one is a lesson about the fake.

1. **`remove_client` is a coroutine** and was called without being awaited, so
   A5 did nothing but log a `RuntimeWarning`. The tests passed because the
   *fake* made it synchronous — a fake whose whole purpose is to reproduce
   upstream's defects diverged from upstream's **signature** and hid one
   instead. The fake is async now, which fails the old code.
2. **Now-playing flapped** between the routed track and nothing every ten
   seconds. A source link is keyed `<unit>:<source>` and shares its unit's
   `server_id`, so the duplicate-link sweep closed it as a second path to a
   server already reached; the next sync rebuilt it, and a rebuilt link reports
   no metadata while still taking precedence.
3. **The dropdown listed bare "Plum Amp100" and "Plum RackPi"** beside those
   units' own streams, because the identity fallback that excludes a unit
   discovered over two addresses compared `unit_id` against `server_id`.

(2) and (3) are the **same root cause the upgrade analysis names**: a unit's
`server_id` is now its X25519 public key rather than a unit-scoped value. §4 of
the upgrade plan recorded that shape change for `client_id` and for player
identity, and missed that the *server* side of it silently broke every place a
unit was recognised by comparing the two. See SPEC-UPGRADE-PLAN §4b.

---

## 1. What "parity with Sendspin 6" means

Per speaker, all seven must hold. This is the acceptance checklist for the whole
piece of work, and every test in §6 maps to a row.

| # | Capability | Mechanism |
|---|---|---|
| P1 | Appears as a `media_player`, durable across restarts and IP changes | discovery + `player_memo` |
| P2 | Home Assistant can take custody on request | hosted server dial |
| P3 | Volume and mute | custody, or Plum's HTTP API for a unit-held speaker |
| P4 | `select_source` onto any live stream | Plum HTTP API |
| P5 | Hand to another server, and take back | release + dial |
| P6 | Now-playing, artwork, transport | `legacy_client.py` |
| P7 | Availability reflects real presence | coordinator + mesh view |

🔬 **P4, P6 and P7 already work and are untouched by this plan.** P6 was
re-measured against all three upgraded servers on 2026-08-15 and needs no
change. The work below is P2, P3 and P5 — all three of which reduce to a single
question: *can Home Assistant take custody of a speaker?*

---

## 2. Where each endpoint class stands

🔬 Measured 2026-08-15, every speaker on the network, dialed unpaired from a
throwaway server.

| Endpoint | Custody on `DISCOVERY` | Custody on `PLAYBACK` | Gap |
|---|---|---|---|
| `esparagus-hifi-1` (ESPHome) | ❌ `ANOTHER_SERVER` | ✅ held, `player@v1` + `controller@v1` + `metadata@v1` | dial reason only |
| `satellite1-d09ee8` (ESPHome) | ❌ `ANOTHER_SERVER` | ✅ held, `player@v1` + `controller@v1` | dial reason only |
| `home-assistant-voice-09472d` (Voice PE) | ❌ `ANOTHER_SERVER` | ✅ held, `player@v1` + `controller@v1` | dial reason only |
| `player-7204` (Plum Amp100) | ❌ `CONCURRENT_ATTEMPT` | untested, expected ❌ | arbitration rank |
| `player-7122` (Plum RackPi) | ❌ `CONCURRENT_ATTEMPT` | ❌ `CONCURRENT_ATTEMPT` | arbitration rank |

Two distinct problems, and they want different fixes:

- **Cleartext endpoints** honour the connection reason. One constant fixes all of
  them. → Track A.
- **Plum players** ignore the connection reason, because the legacy path declares
  no activities at all, so the rank is 0 by construction. No dial we can make
  from the current code wins. → Track B.

---

## 3. Track A — the dial reason

**This is the whole cleartext fleet, and it is a one-constant change.**

`async_adopt` dials `ConnectionReason.DISCOVERY` (`server_host.py:214-219`),
which 🔬 every speaker on the network refuses. `sendspin.reclaim_player` works
today *only* because `reclaim_client_for_playback()` dials `PLAYBACK`
(`server/server.py:673-693`).

### A1 — `async_adopt` dials `PLAYBACK`

The `DISCOVERY` choice was deliberate politeness (`OPEN-QUESTIONS.md` §1): a
`PLAYBACK` dial asserts a claim Home Assistant has no audio to justify. 🔬 That
tradeoff no longer exists — `DISCOVERY` does not yield a gentler outcome, it
yields *no* outcome. The protection that actually mattered is untouched:

- nothing is ever auto-dialed; adoption stays an explicit, warned user action
- `ANOTHER_SERVER` still ends the dial rather than starting a tug-of-war
- `sendspin.release_player` still hands a speaker back

### A2 — `CONCURRENT_ATTEMPT` becomes non-retryable

Add it to `_NON_RETRYABLE_GOODBYES` (`server_host.py:80-86`). 🔬 It is the only
goodbye an upgraded Plum player sends, and today it falls through to the flap
counter — three rounds of hammering a speaker MA is using before yielding with a
vague "contested". Surface the holding server's name, which the mesh view already
gives us as `local_player.server_name`.

### A3 — `reclaim` collapses into `adopt`

Once both dial `PLAYBACK`, `async_reclaim` differs from `async_adopt` only in
clearing the yield and arming a timeout (`server_host.py:229-240`) — and
`async_adopt` already clears the yield (`:211`). Keep the service for its
explicit "take it anyway" intent and its timeout, but implement it as
`async_adopt` plus the timeout rather than a second dialing path. **No service is
removed** — `services.yaml`, `icons.json` and the translations stay as they are.

### A4 — diagnostics

🔬 Every fact that decided this pass — `active_roles`, trust level,
`unpaired_access`, the last goodbye reason — is invisible today without attaching
a debugger. Surface all four per client in `diagnostics.py`, which already
reports `server_id` and link snapshots.

### A5 — `remove_client()` on yield

Closes the retained-object leak (`SPEC-UPGRADE-PLAN.md` §4). Small, unrelated to
the rest, bundled here because it touches the same yield path.

**Expected outcome of Track A alone: P2, P3 and P5 restored for every cleartext
endpoint — 3 of 5 speakers today, and every third-party endpoint that has not
moved to 9.x.**

---

## 4. Track B — Plum player custody, and how to avoid pairing

Plum players are the only endpoints Track A does not fix. Four options, cheapest
first. **This is the decision that needs your input**, and §5's gate tests are
designed to make it for us.

### B0 — Accept it. Do nothing.

🔬 Plum players are *already* routable and controllable without custody: `select_source`
routes them through Plum's HTTP API (`mesh.py:316`), volume goes through
`/api/mesh/volume`, and now-playing comes from the controller link. The only
things missing are parking a speaker on `Source: None` and HA-side volume for a
speaker HA holds — both of which have working equivalents.

**Cost: zero. Gap: P2 for two speakers, with P3 and P5 covered another way.**

### B1 — Plum treats a cleartext dial the way the ESPs do — *recommended*

The ESPHome speakers honour `connection_reason` and hand themselves over on a
`PLAYBACK` dial. Plum's player refuses because the legacy path never sends
`server/activate`, so arbitration sees rank 0 (`client/client.py:661-679`).

Since Plum's cleartext listener is **Plum's own code, not the library's**
(`SPEC-UPGRADE-PLAN.md` §2), Plum can treat a legacy `server/hello` carrying
`connection_reason: playback` as a playback claim — which is exactly the
Sendspin-6 semantics being restored, and exactly what the ESPs do.

**Cost: a Plum-side change, no HA-Sendspin work at all, no pairing.** This is the
cheapest route to full parity and it is entirely within your control.

### B2 — Plum enables `unpaired_access`, HA calls `trust_unpaired`

🔬 The player currently advertises `UnpairedAccess(enabled=False)`. If Plum sets
`unpaired_access_enabled=True` (`noise/trust_store.py:191`), then a sentinel-PSK
connection becomes playback-capable once **our** side also approves it via
`server.trust_unpaired(client_id)` (`server/server.py:597-601`) — both halves are
required (`server/connection.py:1096-1099`). That reaches rank 2 with **no token,
no PIN, no pairing exchange**: a checkbox at adoption time.

**Two preconditions, both Plum-side**: the flag, *and* the player must connect
over Noise rather than cleartext — a sentinel PSK only exists on an encrypted
connection. Today it chooses cleartext, so B2 needs both changes where B1 needs
one.

**Cost: one HA-Sendspin checkbox + a `trust_unpaired` call, plus two Plum-side
changes.**

### B3 — Pairing in the setup flow

The fallback, and the only option that works for a **third-party** encrypted
endpoint we do not control. Detailed in §7. Build it if a gate test says B1/B2
are unavailable, or when a third encrypted endpoint appears.

**Cost: a config-flow step, persistence, and a new failure surface.**

### How pairing bootstraps against a held player, if it comes to that

Worth recording because it is not obvious: `Activity.PAIRING` ranks **1**, which
beats an idle incumbent's 0 (`client/client.py:650-659`). So a pairing dial can
displace an idle MA, complete the exchange, and a subsequent paired `PLAYBACK`
dial then holds at rank 2. Pairing is not blocked by the thing it fixes.

---

## 5. Decision gates — tests that choose the track

Run **before** building anything in Track B. Each is a probe script run, no code
change, ~2 minutes.

| Gate | Test | If it passes | If it fails |
|---|---|---|---|
| **G1** | Free a Plum player from MA (disable that player in MA), then dial cleartext `PLAYBACK` with `probe_dial.py` | Custody works whenever the speaker is uncontested → **B0 is enough for most use**; no pairing | Plum players need B1, B2 or B3 |
| **G2** | With HA holding it from G1, re-enable it in MA and see whether MA takes it back | Custody is stable → B0 fully sufficient | Custody is unstable under contention → need rank 2, so B1/B2/B3 |
| ~~**G3**~~ | ~~Confirm the `PLAYBACK` result on the other two ESPHome speakers~~ | ✅ **passed 2026-08-15** — both held with `player@v1` + `controller@v1`, no goodbye | — |

G1 and G2 need MA's player settings touched, so they want a moment when nothing
is playing.

🔬 **G3 is done: Track A is confirmed to cover all three ESPHome endpoints.** Only
G1 and G2 remain, and both are about Plum players specifically.

---

## 6. Test plan

### 6.1 Unit tests — `pytest-homeassistant-custom-component`

New file `tests/test_server_host.py`, against a mocked `SendspinServer`. All are
TDD-shaped: each asserts a behaviour that is currently wrong.

| Test | Asserts |
|---|---|
| `test_adopt_dials_for_playback` | `connect_to_client` called with `ConnectionReason.PLAYBACK` |
| `test_concurrent_attempt_yields_immediately` | one `CONCURRENT_ATTEMPT` disconnect yields; does **not** wait for `_FLAP_THRESHOLD` |
| `test_concurrent_attempt_names_holder` | the yield reason surfaces the holding server, not `contested` |
| `test_another_server_still_yields` | Critical Rule 5's conclusion is not regressed |
| `test_flap_counter_still_fires_without_goodbye` | the no-goodbye path is untouched |
| `test_adopt_clears_previous_yield` | re-adopting a yielded URL retries |
| `test_yield_removes_client` | `remove_client()` called (A5) |
| `test_zero_active_roles_marks_pending` | a connected-but-inert client is not reported as working |

Existing `tests/` must stay green — in particular anything asserting the
`DISCOVERY` reason will fail by design and is the change being made.

### 6.2 Live tests — one disruption window

Requires `./scripts/ha_probe.sh cycle` (restarts the live instance) and debug
logging re-applied afterwards, since `logger.set_level` does not survive a
restart. **Confirm before running.** Run with nothing playing.

| # | Test | Parity row | Pass |
|---|---|---|---|
| L1 | Adopt each ESPHome speaker through the integration | P2 | entity holds, `active_roles` non-empty in diagnostics |
| L2 | Volume and mute on an HA-held ESP | P3 | speaker responds; state round-trips |
| L3 | `select_source` an ESP onto a Plum stream | P4 | mesh view shows it on the source's group |
| L4 | `Source: None` on that ESP | P5 | HA takes it back and holds it |
| L5 | Select another server as the source, then take it back | P5 | hand-off, then recovery |
| L6 | Adopt a Plum player | P2 | yields cleanly naming MA — **not** three rounds of flapping |
| L7 | Now-playing from a Plum unit and from MA, with something playing | P6 | title/artist/artwork/progress |
| L8 | Restart HA | P1, P7 | entities return, custody re-established, no re-adoption prompts |
| L9 | Adopt an ESP while a Plum controller link is live | §2 | both work on one server instance |

L7 is also the outstanding `ctrl:<unit>:<source>` check from the upgrade plan's
§8, which could not run because nothing was playing during the measurement pass.

### 6.3 Regression watch

- `ruff check .` and `ruff format .` clean.
- No change to `manifest.json`, `const.py` service names, `services.yaml`,
  `strings.json` or the translations — this work adds no user-facing service and
  removes none.

---

## 7. Track C — pairing in the setup flow, if a gate demands it

Contingent on §5. Scoped to the minimum that works, per the stated preference.

- **Pairing-PSK token only.** 🔬 The player offers `PAIRING_PSK` and
  `DYNAMIC_PIN`; static PIN is not offered, so the 8-digit gesture-gated path is
  already off the table. `PAIRING_PSK` advertises
  `locations: ['device', 'operator']`, so **HA mints the token and the user
  pastes it into Plum's GUI** — one generated string to display, no parsing, no
  PIN entry, no countdown UI.
- **New optional step in `PlayerSubentryFlow`** (`config_flow.py`), shown only
  when the discovered endpoint is encrypted. ESPs never see it.
- **`stage_pairing_psk()` before the first `connect_to_client`** — never on a
  live connection. 🗣️ Pairing an already-connected client forces a mid-connection
  re-handshake that loses the race against a contended player.
- **Never attach a `pairing_attempt` to a dial that might land cleartext** — the
  library aborts with "pairing requires an encrypted connection"
  (`server/connection.py:1209`).
- **Persistence is already correct** and must not be disturbed: `identity.py`
  stores the X25519 private key via HA's `Store` and pairing records via
  `FileServerPairingStore`. Rotating the key invalidates every pairing even with
  the store intact.
- **Expect the cleartext path to that player to stop working afterwards.**
  Downgrade protection refuses an unencrypted hello claiming a `client_id` we
  hold a record for (`server/connection.py:1067-1086`). By design, not a
  regression.

Additional unit tests: token round-trip through `decode_token()`, the flow step
being skipped for a cleartext peer, and a staged PSK surviving a restart.

---

## 8. Sequencing and approval

| Order | Item | Depends on | State |
|---|---|---|---|
| 1 | G3 gate test | — | ✅ passed |
| 2 | **Track A** (A1–A5) + unit tests §6.1 | — | ✅ shipped v0.3.7 |
| 3 | Live tests §6.2 | Track A merged | ◐ partly — see below |
| 4 | G1 + G2 gate tests | MA player settings | ☐ **next**, needs a quiet window |
| 5 | Track B decision | G1, G2 | ☐ your call between B0/B1/B2/B3 |
| 6 | Track C, only if B3 | step 5 | ☐ scope already agreed |

### Live tests, as actually run

| # | Test | Result |
|---|---|---|
| L1 | Adopt an ESPHome speaker through the integration | ✅ routing and control confirmed by hand |
| L3 | `select_source` onto a Plum stream | ✅ works for ESPs and Plum endpoints |
| L6 | Adopt a Plum player | ✅ yields cleanly naming `concurrent_attempt`, first refusal, no flapping |
| L7 | Now-playing from a Plum unit with something playing | ✅ after v0.3.9 — stable title, artist and artwork |
| L2, L4, L5, L8, L9 | volume/mute, `Source: None`, server hand-off, restart persistence, mixed encrypted + cleartext | ☐ not yet exercised deliberately |

L7 also closes the outstanding `ctrl:<unit>:<source>` item in
SPEC-UPGRADE-PLAN §8: targeting **works** on 9.1.x. A link with a
`ctrl:<source>:<nonce>` id lands in the named source's group and receives
title, artist and artwork.

### Rollback

Track A is four small edits in one file plus a diagnostics addition. Revert the
commit and restart. Nothing persists to disk, no storage schema changes, no
entity-registry effects — a reverted A1 simply goes back to a dial that is
refused.

---

## 9. Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| HA now actively takes speakers from MA where it previously did not | **certain — this is the point** | adoption remains explicit-consent-only; `release_player` hands back; documented in README |
| A `PLAYBACK` dial is refused by some endpoint class not yet tested | low | G3 covers the two untested ESPs; any third-party endpoint is unknown until it appears |
| Two servers both dialing `PLAYBACK` produce a tug-of-war | medium | `ANOTHER_SERVER` still ends the dial; flap counter still backstops the no-goodbye case |
| Plum-side change (B1/B2) never happens | medium | B0 is a genuine fallback — Plum routing, volume and metadata already work |
| Downgrade protection strands a paired Plum player if Plum reverts to cleartext | low | only reachable via Track C; documented in §7 |
