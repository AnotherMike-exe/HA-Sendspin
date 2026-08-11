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
