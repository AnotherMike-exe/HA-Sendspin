# Brand assets

HACS **requires** a `brand/` directory containing at least `icon.png` for a custom
integration to publish. These are binary assets and are not scaffolded.

Required:

| File | Size | Notes |
|---|---|---|
| `icon.png` | 256×256 | Square, transparent background. Required. |
| `icon@2x.png` | 512×512 | Optional hDPI variant. |
| `logo.png` | max 512×512 | Optional. Wordmark; used where a wide logo fits better. |
| `logo@2x.png` | max 1024×1024 | Optional hDPI variant. |

Reference: <https://developers.home-assistant.io/docs/creating_integration_file_structure#brand-images---brand>

> Delete this README once the assets are in place — HACS only looks for the images.
