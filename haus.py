#!/usr/bin/env python3
"""haus — a cache-first, usage-aware command line for Home Assistant.

Talk to your house by the names you actually use ("desk lamp", "living room
blinds"), not by entity ids. The first run builds a local index of controllable
entities plus 30 days of logbook activity; after that every command resolves a
spoken name against that index, ranks candidates by how often they are really
used, calls the service and (optionally) waits for the new state.

Designed for humans and for AI agents: one short command in, one short line out.

Commands
    haus sync [--days N] [--full]     rebuild/refresh the local index
    haus status                      index age, entity counts, top devices
    haus find <text>                 ranked candidates with live state + usage
    haus on|off|toggle <text|group>  switch things
    haus open|close|stop <text|group>   covers
    haus lock|unlock <text|group>
    haus position --to <0-100> <text|group>
    haus lights|covers|switches|climates|fans|media <action>   whole domain
    haus demo                        harmless tour: status + find + dry-runs

Global flags
    --dry-run        resolve and print the calls, execute nothing
    --no-wait        do not poll for the new state (fast, unverified)
    --json           machine-readable output
    --lang en|hr     spoken-alias language pack (default: en)
    --config PATH    config file (default: ~/.config/haus/config.json)
    --cache PATH     index file (default: per-OS cache dir)

Config (first match wins, env beats file)
    HA_URL / HA_TOKEN env vars, or
    {"url": "http://homeassistant.local:8123", "token": "...", "lang": "en",
     "window_days": 30} in the config file.

MIT licensed. See README.md.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

VERSION = "0.1.0"
CONTROLLABLE = {
    "light", "switch", "cover", "climate", "fan", "media_player", "lock",
    "scene", "script", "vacuum", "humidifier", "water_heater", "input_boolean",
}
DOMAIN_PRIORITY = {"light": 0, "cover": 1, "climate": 2, "fan": 3,
                   "media_player": 4, "water_heater": 5, "humidifier": 6,
                   "input_boolean": 7, "switch": 8, "lock": 9,
                   "scene": 10, "script": 11, "vacuum": 12}
ACTIVE_STATES = {"on", "open", "opening", "playing", "heat", "cool", "heat_cool",
                 "auto", "dry", "fan_only", "unlocked", "home", "cleaning"}
SERVICE_FOR = {
    "on": {"light": "turn_on", "switch": "turn_on", "fan": "turn_on",
           "input_boolean": "turn_on", "climate": "turn_on",
           "media_player": "turn_on", "humidifier": "turn_on",
           "water_heater": "turn_on"},
    "off": {"light": "turn_off", "switch": "turn_off", "fan": "turn_off",
            "input_boolean": "turn_off", "climate": "turn_off",
            "media_player": "turn_off", "humidifier": "turn_off",
            "water_heater": "turn_off"},
    "toggle": {"light": "toggle", "switch": "toggle", "fan": "toggle",
               "input_boolean": "toggle"},
    "open": {"cover": "open_cover"},
    "close": {"cover": "close_cover"},
    "stop": {"cover": "stop_cover"},
    "lock": {"lock": "lock"},
    "unlock": {"lock": "unlock"},
    "position": {"cover": "set_cover_position"},
}
EXPECTED = {"on": {"on"}, "off": {"off"}, "open": {"open", "opening"},
            "close": {"closed", "closing"}, "lock": {"locked"},
            "unlock": {"unlocked"}}

# spoken aliases, per language -------------------------------------------------
LANG = {
    "en": {
        "groups": {
            "lights": "light", "light": "light",
            "covers": "cover", "cover": "cover", "blinds": "cover",
            "shutters": "cover", "curtains": "cover",
            "switches": "switch", "switch": "switch", "outlets": "switch",
            "climates": "climate", "climate": "climate", "heating": "climate",
            "fans": "fan", "fan": "fan",
            "media": "media_player", "speakers": "media_player",
        },
        "filler": {"all", "the", "a", "an", "please", "turn", "set",
                   "switch", "make"},
        "generic": {"light", "lights", "lamp", "lamps", "switch", "switches",
                    "cover", "covers", "blind", "blinds", "shutter",
                    "shutters", "the", "a", "an"},
    },
    "hr": {
        "groups": {
            "svjetla": "light", "svjetlo": "light", "lampe": "light",
            "lampa": "light",
            "rolete": "cover", "roleta": "cover", "zavjese": "cover",
            "prekidaci": "switch", "uticnice": "switch", "releji": "switch",
            "klima": "climate", "grijanje": "climate",
            "ventilatori": "fan",
            "media": "media_player", "zvucnici": "media_player",
        },
        "filler": {"sva", "sve", "svi", "molim", "upali", "ugasi", "otvori",
                   "zatvori", "podigni", "spusti", "i"},
        "generic": {"svjetlo", "svjetla", "lampa", "lampe", "prekidac",
                    "uticnica", "roleta", "rolete", "zavjesa", "zavjese"},
    },
}


# --------------------------------------------------------------------- config --
def default_cache_path() -> str:
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Caches/haus")
    else:
        base = os.path.join(os.environ.get("XDG_CACHE_HOME",
                                           os.path.expanduser("~/.cache")), "haus")
    return os.path.join(base, "index.json")


def load_config(path: str | None = None) -> dict:
    cfg_path = path or os.environ.get("HAUS_CONFIG") or \
        os.path.expanduser("~/.config/haus/config.json")
    cfg = {}
    try:
        with open(cfg_path) as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        pass
    except json.JSONDecodeError as exc:
        raise SystemExit(f"config {cfg_path}: {exc}")
    cfg["url"] = os.environ.get("HA_URL") or cfg.get("url")
    cfg["token"] = os.environ.get("HA_TOKEN") or cfg.get("token")
    cfg["lang"] = os.environ.get("HAUS_LANG") or cfg.get("lang") or "en"
    cfg["window_days"] = int(cfg.get("window_days") or 30)
    cfg["cache"] = os.environ.get("HAUS_CACHE") or cfg.get("cache") or default_cache_path()
    cfg["config_path"] = cfg_path
    return cfg


def norm(text: str) -> str:
    text = text or ""
    # characters NFKD does not decompose
    for src, dst in (("đ", "d"), ("Đ", "D"), ("ø", "o"), ("Ø", "O"),
                     ("ł", "l"), ("Ł", "L"), ("ß", "ss")):
        text = text.replace(src, dst)
    text = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in text if not unicodedata.combining(ch)).lower()


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", norm(text))


def tidy_name(name: str, eid: str) -> str:
    """Collapse doubled friendly names ("Blind X Blind X") that some stacks emit."""
    name = (name or eid).strip()
    words = name.split()
    half = len(words) // 2
    if len(words) >= 4 and len(words) % 2 == 0 and words[:half] == words[half:]:
        name = " ".join(words[:half])
    return name


# ------------------------------------------------------------------------ api --
class Api:
    def __init__(self, cfg: dict):
        if not cfg.get("url") or not cfg.get("token"):
            raise SystemExit(
                "Home Assistant is not configured.\n"
                "Set HA_URL and HA_TOKEN, or write ~/.config/haus/config.json:\n"
                '  {"url": "http://homeassistant.local:8123", "token": "<long-lived token>"}')
        self.url = cfg["url"].rstrip("/")
        self.token = cfg["token"]

    def request(self, path: str, method: str = "GET", payload=None, timeout: int = 60):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            self.url + path, method=method, data=data,
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise SystemExit(f"HA API {method} {path}: HTTP {exc.code} {detail}")
        except urllib.error.URLError as exc:
            raise SystemExit(f"cannot reach Home Assistant at {self.url}: {exc.reason}")
        return (json.loads(body) if body else None), (time.time() - t0) * 1000

    def states(self) -> list[dict]:
        data, _ = self.request("/api/states")
        return data

    def state(self, eid: str) -> dict:
        data, _ = self.request(f"/api/states/{eid}", timeout=15)
        return data

    def call(self, domain: str, service: str, entity_ids: list[str], data: dict | None):
        payload = {"entity_id": entity_ids}
        if data:
            payload.update(data)
        return self.request(f"/api/services/{domain}/{service}", "POST", payload)

    def logbook(self, start: datetime, end: datetime) -> list[dict]:
        start_iso = urllib.parse.quote(
            start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"), safe="")
        end_iso = urllib.parse.quote(
            end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"), safe="")
        data, _ = self.request(f"/api/logbook/{start_iso}?end={end_iso}", timeout=120)
        return data or []


# ---------------------------------------------------------------------- index --
def parse_when(value: str):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fetch_entities(api: Api) -> dict:
    raw = {}
    for st in api.states():
        eid = st["entity_id"]
        domain = eid.split(".")[0]
        if domain not in CONTROLLABLE:
            continue
        name = tidy_name(st["attributes"].get("friendly_name") or eid, eid)
        raw[eid] = {
            "name": name,
            "domain": domain,
            "state": st["state"],
            "name_tokens": sorted(set(tokens(name))),
            "tokens": sorted(set(tokens(name + " " + eid.replace(".", " ")))),
            "activations": 0,
            "changes": 0,
            "last": None,
        }
    return raw


def dedupe(entities: dict) -> tuple[dict, dict]:
    """One physical device is often exposed twice (light + switch through a
    switch-as-light helper). Group by NAME tokens, keep the richer domain."""
    by_name: dict[tuple, list[str]] = {}
    for eid, rec in entities.items():
        by_name.setdefault(tuple(rec["name_tokens"]), []).append(eid)
    keep: dict[str, dict] = {}
    dropped: dict[str, str] = {}
    for _key, ids in by_name.items():
        ids.sort(key=lambda e: (DOMAIN_PRIORITY.get(e.split(".")[0], 99), e))
        keep[ids[0]] = entities[ids[0]]
        for other in ids[1:]:
            dropped[other] = ids[0]
    return keep, dropped


def fold_usage(entities: dict, keep: dict, dropped: dict) -> None:
    """Mirror entities log the same physical event — take max, never sum."""
    for other, primary in dropped.items():
        dup, rec = entities[other], keep[primary]
        rec["activations"] = max(rec["activations"], dup["activations"])
        rec["changes"] = max(rec["changes"], dup["changes"])
        if dup["last"] and (not rec["last"] or dup["last"] > rec["last"]):
            rec["last"] = dup["last"]


def merge_logbook(entities: dict, entries: list[dict]) -> None:
    for entry in entries:
        rec = entities.get(entry.get("entity_id"))
        if not rec or "state" not in entry:
            continue
        rec["changes"] += 1
        when = entry.get("when")
        if when and (rec["last"] is None or when > rec["last"]):
            rec["last"] = when
        if str(entry.get("state", "")).lower() in ACTIVE_STATES:
            rec["activations"] += 1


def build_groups(entities: dict) -> dict:
    groups: dict[str, list[str]] = {}
    for eid, rec in entities.items():
        groups.setdefault(rec["domain"], []).append(eid)
    return {k: sorted(v) for k, v in groups.items()}


def load_cache(path: str):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_cache(path: str, cache: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(cache, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def sync(cfg: dict, full: bool = False, days: int | None = None) -> dict:
    api = Api(cfg)
    days = days or cfg["window_days"]
    old = None if full else load_cache(cfg["cache"])
    entities = fetch_entities(api)
    if old:
        for eid, rec in entities.items():
            prev = old.get("entities", {}).get(eid)
            if prev:
                rec["activations"] = prev.get("activations", 0)
                rec["changes"] = prev.get("changes", 0)
                rec["last"] = prev.get("last")
        start = parse_when(old.get("generated")) or \
            datetime.now(timezone.utc) - timedelta(days=days)
        start -= timedelta(minutes=5)
    else:
        start = datetime.now(timezone.utc) - timedelta(days=days)
    end = datetime.now(timezone.utc)

    cursor, chunk = start, timedelta(days=3)
    while cursor < end:
        stop = min(cursor + chunk, end)
        merge_logbook(entities, api.logbook(cursor, stop))
        cursor = stop

    keep, dropped = dedupe(entities)
    fold_usage(entities, keep, dropped)
    cache = {
        "generated": end.isoformat(),
        "window_days": days,
        "count": len(keep),
        "duplicates": dropped,
        "entities": keep,
        "groups": build_groups(keep),
    }
    save_cache(cfg["cache"], cache)
    return cache


def ensure_cache(cfg: dict, max_age_hours: float = 12.0) -> dict:
    cache = load_cache(cfg["cache"])
    if not cache:
        return sync(cfg, full=True)
    generated = parse_when(cache.get("generated"))
    if generated and (datetime.now(timezone.utc) - generated).total_seconds() > max_age_hours * 3600:
        try:
            return sync(cfg, full=False)
        except SystemExit as exc:
            print(f"warning: refresh failed ({exc}); using the cached index", file=sys.stderr)
    return cache


# ------------------------------------------------------------------- matching --
def score(rec: dict, query_tokens: list[str]) -> float:
    if not query_tokens:
        return 0.0
    name_tokens = set(rec["tokens"])
    if not all(t in name_tokens for t in query_tokens):
        joined = " ".join(query_tokens)
        if joined not in norm(rec["name"]) and joined not in " ".join(rec["tokens"]):
            return 0.0
    base = 10.0 - 0.15 * max(0, len(name_tokens) - len(query_tokens))
    base += min(6.0, (rec.get("activations", 0) or 0) ** 0.5)
    if rec.get("state") in ("unavailable", "unknown"):
        base -= 8.0
    return base


def resolve(cache: dict, query: str) -> list[tuple[float, str, dict]]:
    q = tokens(query)
    hits = [(score(rec, q), eid, rec) for eid, rec in cache["entities"].items()]
    hits = [h for h in hits if h[0] > 0]
    hits.sort(key=lambda h: (-h[0], h[1]))
    return hits


def group_targets(cache: dict, target: str, action: str, lang: dict):
    low = target.lower().strip()
    packs = lang["groups"]
    if low in ("all", "everything", "sve", "sva", "svi"):
        wanted = set(SERVICE_FOR[action])
        return [eid for eid, rec in cache["entities"].items()
                if rec["domain"] in wanted
                and rec.get("state") not in ("unavailable", "unknown")]
    toks = [t for t in tokens(target) if t not in lang["filler"]]
    candidates = [low, " ".join(toks), " ".join(t for t in toks if t not in lang["generic"])]
    for cand in candidates:
        if cand in packs:
            domain = packs[cand]
            if domain not in SERVICE_FOR[action]:
                raise SystemExit(f"'{action}' is not supported for {domain}")
            ids = [eid for eid in cache["groups"].get(domain, [])
                   if cache["entities"][eid].get("state") not in ("unavailable", "unknown")]
            if not ids:
                raise SystemExit(f"group '{cand}' has no available entities")
            return ids
    return []


def expand(cache: dict, target: str, action: str, lang: dict) -> list[str]:
    grouped = group_targets(cache, target, action, lang)
    if grouped:
        return grouped
    hits = resolve(cache, target)
    if not hits:
        raise SystemExit(f"nothing matches {target!r}")
    if len(hits) > 1 and hits[0][0] - hits[1][0] < 1.5:
        lines = [f"  {eid}  ({rec['name']}, {rec['state']})" for _, eid, rec in hits[:6]]
        raise SystemExit("ambiguous — be more specific:\n" + "\n".join(lines))
    return [hits[0][1]]


# --------------------------------------------------------------------- output --
def emit(args, payload: dict, human: str) -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(human)


def cmd_status(args, cfg: dict, lang: dict) -> int:
    cache = load_cache(cfg["cache"])
    if not cache:
        raise SystemExit("no index yet — run `haus sync`")
    ents = cache["entities"]
    top = sorted(ents.items(), key=lambda kv: -kv[1].get("activations", 0))[:12]
    payload = {
        "cache": cfg["cache"],
        "generated": cache["generated"],
        "entities": len(ents),
        "window_days": cache["window_days"],
        "groups": {k: len(v) for k, v in sorted(cache["groups"].items())},
        "top": [{"entity_id": eid, "name": rec["name"], "activations": rec["activations"]}
                for eid, rec in top],
    }
    lines = [f"index: {cfg['cache']}",
             f"generated: {cache['generated']}  entities: {len(ents)}  window: {cache['window_days']}d",
             "groups: " + ", ".join(f"{k}={len(v)}" for k, v in sorted(cache["groups"].items())),
             "most used:"]
    lines += [f"  {rec['activations']:4d}x  {eid:48s} {rec['name']}" for eid, rec in top]
    emit(args, payload, "\n".join(lines))
    return 0


def cmd_find(args, cfg: dict, lang: dict) -> int:
    cache = ensure_cache(cfg)
    hits = resolve(cache, args.text)[:8]
    payload = {"query": args.text,
               "matches": [{"entity_id": eid, "name": rec["name"], "state": rec["state"],
                            "activations": rec["activations"], "last": rec["last"],
                            "score": round(sc, 2)} for sc, eid, rec in hits]}
    if not hits:
        emit(args, payload, "no match")
        return 1
    lines = [f"{sc:5.1f}  {eid:44s} {rec['state']:12s} used={rec['activations']:4d}  {rec['name']}"
             for sc, eid, rec in hits]
    emit(args, payload, "\n".join(lines))
    return 0


def wait_state(api: Api, ids: list[str], expected: set, timeout: float = 2.5):
    t0 = time.time()
    last: dict[str, str] = {}
    while True:
        for eid in ids[:25]:
            try:
                last[eid] = api.state(eid).get("state", "?")
            except SystemExit:
                last[eid] = "?"
        if last and all(v in expected for v in last.values()):
            return last, True, (time.time() - t0) * 1000
        if time.time() - t0 >= timeout:
            return last, False, (time.time() - t0) * 1000
        time.sleep(0.2)


def cmd_control(args, cfg: dict, lang: dict, action: str) -> int:
    cache = ensure_cache(cfg)
    ids = expand(cache, args.target, action, lang)
    api = Api(cfg)
    by_domain: dict[str, list[str]] = {}
    for eid in ids:
        by_domain.setdefault(eid.split(".")[0], []).append(eid)
    if args.dry_run:
        payload = {"dry_run": True, "action": action, "entities": ids}
        lines = [f"(dry-run) {action}: {len(ids)} entities"]
        for domain, group in by_domain.items():
            lines.append(f"  {domain}.{SERVICE_FOR[action].get(domain)}: {', '.join(group)}")
        emit(args, payload, "\n".join(lines))
        return 0
    total_ms = 0.0
    data = {"position": args.position} if action == "position" else None
    for domain, group in by_domain.items():
        service = SERVICE_FOR[action].get(domain)
        if not service:
            raise SystemExit(f"'{action}' is not supported for {domain}")
        _, ms = api.call(domain, service, group, data)
        total_ms += ms
    confirmed = None
    confirm_ms = 0.0
    states: dict[str, str] = {}
    if not args.no_wait and action in EXPECTED:
        states, confirmed, confirm_ms = wait_state(api, ids, EXPECTED[action])
    payload = {"action": action, "entities": ids, "service_ms": round(total_ms),
               "confirmed": confirmed, "confirm_ms": round(confirm_ms), "states": states}
    if args.no_wait:
        human = f"✓ {action}: {len(ids)} entities | service {total_ms:.0f} ms (no wait)"
    else:
        human = (f"{'✓' if confirmed is not False else '⚠'} {action}: {len(ids)} entities | "
                 f"service {total_ms:.0f} ms, state {confirm_ms:.0f} ms "
                 f"({'confirmed' if confirmed else 'unconfirmed'})")
    emit(args, payload, human)
    return 0


def cmd_demo(args, cfg: dict, lang: dict) -> int:
    print("=== status ===")
    cmd_status(args, cfg, lang)
    print("\n=== find 'desk' ===")
    args.text = "desk"
    cmd_find(args, cfg, lang)
    for action, group in (("on", "lights"), ("open", "covers")):
        print(f"\n=== dry-run: {group} {action} ===")
        args.target, args.dry_run = group, True
        try:
            cmd_control(args, cfg, lang, action)
        except SystemExit as exc:
            print(f"  (skipped: {exc})")
    print("\n(nothing was executed)")
    return 0


# ------------------------------------------------------------------------ cli --
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="haus", description="Cache-first, usage-aware Home Assistant CLI.")
    p.add_argument("--version", action="version", version=f"haus {VERSION}")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--lang", help="spoken-alias language pack (en, hr)")
    p.add_argument("--config", help="config file path")
    p.add_argument("--cache", help="index file path")
    p.add_argument("--no-wait", action="store_true", help="do not poll for the new state")
    p.add_argument("--dry-run", action="store_true", help="resolve only, execute nothing")
    p.add_argument("--position", type=int, help="cover position 0-100 (with the position command)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="index age, counts, most used")
    sub.add_parser("demo", help="harmless tour: status + find + dry-runs")

    s = sub.add_parser("sync", help="refresh the index")
    s.add_argument("--full", action="store_true")
    s.add_argument("--days", type=int)

    f = sub.add_parser("find", help="ranked entity search")
    f.add_argument("text")

    for action in ("on", "off", "toggle", "open", "close", "stop", "lock", "unlock"):
        c = sub.add_parser(action, help=f"{action} a device or a group")
        c.add_argument("target", nargs="+")
    pos = sub.add_parser("position", help="set a cover position 0-100")
    pos.add_argument("target", nargs="+")

    for group in ("lights", "covers", "switches", "climates", "fans", "media"):
        g = sub.add_parser(group, help=f"operate the whole {group} group")
        g.add_argument("action", choices=sorted(SERVICE_FOR))
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if args.cache:
        cfg["cache"] = args.cache
    if args.lang:
        cfg["lang"] = args.lang
    lang = LANG.get(cfg["lang"])
    if not lang:
        raise SystemExit(f"unknown language pack {cfg['lang']!r} (have: {', '.join(sorted(LANG))})")
    if args.cmd == "sync":
        cache = sync(cfg, full=args.full, days=args.days)
        print(f"✓ index: {cache['count']} entities → {cfg['cache']}")
        return 0
    if args.cmd == "status":
        return cmd_status(args, cfg, lang)
    if args.cmd == "demo":
        return cmd_demo(args, cfg, lang)
    if args.cmd == "find":
        return cmd_find(args, cfg, lang)
    if args.cmd == "position":
        if args.position is None:
            raise SystemExit("usage: haus position --position <0-100> <target>")
        args.target = " ".join(args.target)
        return cmd_control(args, cfg, lang, "position")
    if args.cmd in SERVICE_FOR:
        args.target = " ".join(args.target)
        return cmd_control(args, cfg, lang, args.cmd)
    if args.cmd in ("lights", "covers", "switches", "climates", "fans", "media"):
        args.target = args.cmd
        return cmd_control(args, cfg, lang, args.action)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
