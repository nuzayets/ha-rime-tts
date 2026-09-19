# Rime TTS for Home Assistant

A HACS custom integration for [Rime](https://rime.ai) text-to-speech, including **incremental text input and streaming MP3 audio** for Assist.

## Features

- Coda (default) and Mist v3 through Rime's recommended `/ws3` API.
- **US East / US West** selection in setup and options. No automatic cross-region fallback.
- Model-specific language and voice pickers populated from Rime's public catalog.
- Speech starts while your conversation agent is still generating text.
- Cancellation closes the WebSocket and stops the text producer.
- Normal `tts.speak` announcements use the same transport.
- UI configuration, options, and API-key reauthentication. No YAML configuration or extra runtime packages.

Requires **Home Assistant 2026.9.0 or newer** and a [Rime API key](https://docs.rime.ai/docs/api-authentication). Rime synthesis is a paid cloud service. The key is stored in Home Assistant's integration configuration, never in this repository.

## Install with HACS

1. Open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/nuzayets/ha-rime-tts`, category **Integration**.
3. Download **Rime TTS** and restart Home Assistant.
4. Open **Settings → Devices & services → Add integration → Rime TTS**.
5. Enter your API key, region, model, language, and default voice.
6. Select the new Rime entity under your voice assistant's **Text-to-speech** settings.

Manual installation: copy `custom_components/rime_tts` into your Home Assistant `custom_components` directory, restart, then add the integration through the UI.

## Regions

| Selection | Streaming endpoint | Catalog endpoint |
| --- | --- | --- |
| `us-east` | `wss://users-east-ws.rime.ai/ws3` | `https://users-east.rime.ai` |
| `us-west` (default) | `wss://users-ws.rime.ai/ws3` | `https://users-west.rime.ai` |

US West matches Rime's default routing. Choose the region closest to your Home Assistant server; US East is usually preferable for Toronto. Change it later with **Configure** on the integration. The initial public voice catalog is loaded from the default region before setup; no credentials or speech are sent in that request. Authentication and synthesis use your selected region.

Each configuration has one model. Add another entry if you want separate Coda and Mist entities. The entity exposes all languages supported by that model. Select a matching voice in Assist or override it in a call; when no voice is supplied for another language, the first matching catalog voice is used.

## Announcements

Replace the entity IDs with those from your installation:

```yaml
action: tts.speak
target:
  entity_id: tts.rime_coda_text_to_speech
data:
  media_player_entity_id: media_player.living_room
  message: "The washing machine has finished."
  language: en
  options:
    voice: astra
```

## Streaming and voice prompts

Rime buffers incoming text to sentence boundaries for natural speech, while audio is returned incrementally. The integration sends end-of-stream when the text producer finishes, so the final phrase is spoken even without punctuation. A `done` event finishes a synthesis batch, not the entire response.

Rime's API authenticates the WebSocket before synthesis; setup validates credentials without generating speech. Interrupted playback closes the connection rather than retrying or replaying partially spoken audio. Connection, authentication, and protocol errors are surfaced to Home Assistant.

Use natural, punctuated text. **Do not reuse Microsoft or Cartesia-specific SSML instructions**: this integration passes speech text through and does not translate another provider's markup. Configure any Rime-specific delivery syntax according to [Rime's documentation](https://docs.rime.ai/docs/api-reference).

Actual streaming requires a streaming conversation agent and a compatible Assist/playback path. A regular `tts.speak` call can still be buffered or cached by Home Assistant. This integration does not add fallback engines, usage sensors, or automatic region switching.

## Development

Install [uv](https://docs.astral.sh/uv/), then:

```sh
uv sync --locked
make check
```

Tests cover the real WebSocket protocol against a local server, cancellation, incremental text/audio, authentication errors, model/language/voice selection, setup/unload, options, and reauthentication. They require no API key or paid synthesis. Development is tested against Home Assistant 2026.9.3.

## References

- [Rime JSON WebSocket API](https://docs.rime.ai/api-reference/coda/websockets-json)
- [Rime regional endpoints](https://docs.rime.ai/docs/regional-endpoints)
- [Home Assistant TTS entity](https://developers.home-assistant.io/docs/core/entity/tts/)

MIT licensed. This is an independent community integration, not an official Rime product.
