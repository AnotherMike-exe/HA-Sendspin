# Changelog

All notable changes to this project are recorded here.

The format is [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Versions before 0.4.0 are described in the
[GitHub releases](https://github.com/AnotherMike-exe/HA-Sendspin/releases).

## [Unreleased]

## [0.5.0] - 2026-11-24

Streams are entities, so a dashboard can show one card for each stream.

**Upgrade notes**

- New `media_player` entities appear, one for each input of each Plum-Audio unit, under a
  device for the unit. Speaker entities do not change.

### Added
- A stream entity for each mesh source: now-playing, cover art, progress and transport for
  the stream, and the `endpoints` and `speakers` attributes that list who is on it.
- Source volume and mute on the stream entity, through `POST /api/mesh/source-volume`, where
  the source accepts one.
- The stream entity's source dropdown moves every adopted speaker on the stream together.
- A controller link for every live stream, so a stream with no adopted speaker still shows
  what plays.

## [0.4.0] - 2026-11-24

The media controls are complete enough for a dashboard media card.

**Upgrade notes**

- A speaker no longer reports the `off` state. It reports `idle`, `playing`, `paused` or
  `unavailable`. Change an automation that tests for `off`.
- A speaker that a Plum unit holds on no stream now shows the source `None`, not the unit name.
- Entity ids and unique ids do not change.

### Added
- Volume step: the volume up and volume down services work, in steps of 5 percent.
- Repeat and shuffle, when the current stream offers them (Spotify).
- The source kind (`app_name` and the `source_kind` attribute).
- Manufacturer, model and software version in the device registry.
- `docs/DASHBOARD.md`, with an example media card and the features of each source kind.

### Changed
- The source list shows only the streams that are live now, plus `None`.
- The transport buttons show only when a stream is selected, and follow what that stream supports.
- The playback state comes from the transport, not from the track title.

### Fixed
- Mute through a Plum unit. Volume and mute now go to the unit as a pair.
- A volume change no longer clears the mute state.
- The track position is correct when a link joins a track that already plays.
- A track title no longer keeps the state at `paused` after the stream ends.
- The cover type comes from the image bytes, so a PNG cover shows.
- `pillow` is no longer in the manifest requirements, so hassfest passes.

[Unreleased]: https://github.com/AnotherMike-exe/HA-Sendspin/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/AnotherMike-exe/HA-Sendspin/releases/tag/v0.5.0
[0.4.0]: https://github.com/AnotherMike-exe/HA-Sendspin/releases/tag/v0.4.0
