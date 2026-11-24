# Dashboard

How to put a Sendspin speaker on a Home Assistant dashboard, and which controls
each kind of source gives.

Each adopted speaker is one `media_player` entity. There are no entities for a
source, a group or a stream. See [OPEN-QUESTIONS §9](OPEN-QUESTIONS.md).

## Example card

The built-in media control card shows the cover, the track, the transport
buttons, the progress bar and the volume. The card shows only the controls that
the entity supports at that moment, so the buttons change with the stream.

```yaml
type: media-control
entity: media_player.kitchen
```

Replace `media_player.kitchen` with the entity id of the speaker. The source
dropdown is in the more-info dialog: tap the name on the card.

To route from an automation or a button, call `media_player.select_source`:

```yaml
action: media_player.select_source
target:
  entity_id: media_player.kitchen
data:
  source: "Plum Amp100 / 204 AP"   # or "None" to take the speaker off the stream
```

## The source dropdown

- The list holds the streams that are live now, and `None`.
- A stream has the label `Unit / Source`, for example `Plum Amp100 / 204 AP`.
  A label does not change when a stream starts or stops. If two sources have the
  same label, each gets its source id: `Plum Amp100 / 204 AP (airplay-1)`.
- To select a stream puts the speaker on that stream. Two speakers on one
  stream are a group. There is no other grouping control.
- `None` takes the speaker off the stream. Home Assistant then holds the
  speaker on no stream, and nothing puts it back on a stream by itself.
- An input with no sender is not in the list. A calibration tone is never in
  the list.
- Another Sendspin server, such as Music Assistant, is also in the list when
  Home Assistant can see it. See [OPEN-QUESTIONS §10](OPEN-QUESTIONS.md).

## Features for each kind of source

The entity reads the commands that the current stream advertises, each time the
state is written. The table shows what Plum-Audio advertises today
(`sendspin_server._supported_commands_for` on the `dev` branch). A cell that
says "if advertised" depends on the server, and the entity follows it live.

| Feature | AirPlay | Bluetooth | Spotify | Other server | None |
|---|---|---|---|---|---|
| Play, pause, next, previous | Yes | Yes | Yes | If advertised | No |
| Stop | No | No | No | If advertised | No |
| Repeat, shuffle | No | If the phone offers it | Yes | If advertised | No |
| Seek | No | No | No | No | No |
| Title, artist, album | If the sender sends it | If the phone sends it | Yes | Yes | No |
| Cover | If the sender sends it | If the phone sends it | Yes | Yes | No |
| Position and duration | If the sender sends it | If the phone sends it | Yes | Yes | No |
| `app_name` | `AirPlay` | `Bluetooth` | `Spotify` | The server name | Absent |
| `source_kind` attribute | `airplay` | `bluetooth` | `spotify` | `server` | Absent |
| Source dropdown | Yes | Yes | Yes | Yes | Yes |

Seek is absent for every source. The Sendspin protocol has no seek command, so
the progress bar is read-only.

Some AirPlay senders send no progress. The entity then has no
`media_position` and no `media_duration`, and the card shows no progress bar.
The state is still `playing` or `paused`.

## Volume

The volume of the entity is the **endpoint** level: the gain of that one
speaker. Plum-Audio has two more volumes, the group level and the source level.
The entity does not read them and does not change them.

| Who holds the speaker | Slider, step and mute |
|---|---|
| Home Assistant | Yes, when the speaker reports its level |
| A Plum-Audio unit | Yes, through `POST /api/mesh/volume` on that unit |
| Another server, such as Music Assistant | No |

One press of volume up or volume down moves the level by 5 percent.

## State

| State | Meaning |
|---|---|
| `playing` | The stream that the speaker is on plays |
| `paused` | The stream that the speaker is on is paused |
| `idle` | The speaker is on no stream, or the stream stopped |
| `unavailable` | Nothing can see the speaker |

A speaker that another server holds stays available. The `held_by` attribute
names the server. The track and the cover show only in `playing` and `paused`.
