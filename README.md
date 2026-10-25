# haus

[![tests](https://github.com/mstipanov/haus-ha/actions/workflows/tests.yml/badge.svg)](https://github.com/mstipanov/haus-ha/actions/workflows/tests.yml)

**A cache-first, usage-aware command line for Home Assistant — built for humans and AI agents.**

Talk to your house by the names you actually use (`desk lamp`, `living room blinds`)
instead of entity ids. `haus` keeps a small local index of what you can control and
how often you really use it, so a command is **one short call** — resolve → execute →
confirm — instead of a discovery dance through 2 800+ entities.

```
$ haus on "desk lamp"
✓ on: 1 entity | service 18 ms, state 363 ms (confirmed)

$ haus open "living room blinds"
✓ open: 3 entities | service 22 ms, state 310 ms (confirmed)

$ haus find "Kitchen"
  13.9  light.kitchen_strip        on        used= 42   Kitchen strip
  11.2  light.kitchen_ceiling      off       used= 18   Kitchen ceiling
```

## Why

Home Assistant is not slow — its REST API answers in ~10 ms. What *is* slow is
everything around a command: finding the entity id, writing an ad-hoc script,
checking whether it worked. `haus` removes those steps and keeps the whole loop
in a single, auditable, dependency-free file.

## Install

```bash
pipx install haus-ha        # or: pip install --user haus-ha
# from a checkout:
pipx install /path/to/haus
```

Requires Python 3.9+ and nothing else (standard library only).

## Configure

Create a long-lived token in Home Assistant: **Profile → Security → Long-lived
access tokens → Create token**.

Either export it:

```bash
export HA_URL="http://homeassistant.local:8123"
export HA_TOKEN="eyJ0eXAiOiJKV1Qi..."
```

or write `~/.config/haus/config.json`:

```json
{
  "url": "http://homeassistant.local:8123",
  "token": "eyJ0eXAiOiJKV1Qi...",
  "lang": "en",
  "window_days": 30
}
```

`lang` selects the spoken-alias pack (`en`, `hr`). Add your own by extending
`LANG` in `haus.py` — group words like *lights/covers/switches* and filler words
like *all/the/please*.

## Commands

| Command | What it does |
|---|---|
| `haus sync [--full] [--days N]` | build/refresh the local index |
| `haus status` | index age, entity counts, most-used devices |
| `haus find <text>` | ranked candidates: score, entity, live state, usage, last used |
| `haus on\|off\|toggle <target>` | switch devices or whole groups |
| `haus open\|close\|stop <target>` | covers |
| `haus lock\|unlock <target>` | locks |
| `haus position --position 30 <target>` | cover position (0 = closed, 100 = open) |
| `haus lights on`, `haus covers open`, … | operate an entire domain |
| `haus demo` | harmless tour: status + find + dry-runs |

Flags: `--dry-run` (resolve only), `--no-wait` (do not poll for the new state),
`--json` (machine-readable output), `--lang`, `--config`, `--cache`.

### How a name is resolved

1. Text is normalised (diacritics stripped, lowercased) and tokenised.
2. Every query token must appear in the entity's name/entity-id tokens.
3. Candidates are ranked by `usage` (square root of activations over the window),
   with a penalty for longer names and for `unavailable` entities.
4. If the two best candidates are within 1.5 points, **the command refuses and
   lists them** — it never guesses and never touches the wrong device.

Group words are matched after filler words are dropped, so `all lights`,
`sva svjetla` (Croatian) and `every blind` all work.

### What the index contains

`haus sync` snapshots controllable entities (light, switch, cover, climate, fan,
media_player, lock, vacuum, …) and folds in `window_days` of logbook activity:

- `activations` — how often the entity entered an active state (`on`, `open`,
  `playing`, `heat`, …)
- `last` — the newest activity timestamp
- `duplicates` — entities that mirror the same physical device

One device is often exposed twice (Zigbee2MQTT plus a *switch-as-light* helper:
`switch.kitchen` **and** `light.kitchen`). `haus` collapses them by **name
tokens**, keeps the richer domain (`light` > `cover` > `climate` > … > `switch`)
and folds usage counters with `max` — both entities log the same physical event,
so summing would double it.

The index refreshes itself when it is older than 12 hours (incremental logbook
fetch). There is no cron to install and nothing to curate by hand.

## Caveats

- **Logbook retention.** Usage numbers only cover what the recorder still has.
  If `purge_keep_days` is shorter than `window_days`, you simply get a shorter
  history — nothing breaks. Keep the recorder DB on SSD-backed storage: a
  30-day logbook sync on a busy house is a few hundred MB of JSON.
- **`assumed_state` players** (e.g. LG webOS TVs) cannot confirm commands; the
  service call succeeds but the state never changes. Use `--no-wait` there.
- **Mass actions are real actions.** `haus lights off` switches every light.
  Run it with `--dry-run` first.
- The index stores entity names and usage counts — keep it out of version
  control and out of public repos.

## Development

```bash
python3 -m unittest discover -s tests
python3 tools/bench.py --query "desk lamp" --cycles 3   # naive path vs haus
```

`tools/bench.py` measures the difference that matters: turns and bytes, not
milliseconds. Home Assistant answers in ~10 ms; an agent that has to discover,
call and verify spends three model round-trips instead of one.

## License

MIT — see [LICENSE](LICENSE).
