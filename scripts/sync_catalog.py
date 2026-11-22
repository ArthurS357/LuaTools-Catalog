"""Merge the public lua.tools Fixes listing with orphaned_fixes.json into catalog.json.

Fail-closed: anything unexpected (download error, bad JSON, schema drift, a suspicious shrink, a bad
override entry) aborts with exit code 1 and leaves the existing catalog.json byte-for-byte untouched.
catalog.json is rewritten only when its content (ignoring ``generated_at``) actually changed, so the
scheduled workflow commits only real changes. Standard library only; no keys, no tokens, no secrets.

Usage:
    python scripts/sync_catalog.py            # download, merge, write
    python scripts/sync_catalog.py --check    # offline: validate orphaned_fixes.json against catalog.json
"""

from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
UPSTREAM_URL = "https://lua.tools/api/denuvo/listings"
UPSTREAM_HOST = "lua.tools"
MAX_BYTES = 8 * 1024 * 1024  # the real listing is ~0.6 MB
TIMEOUT_SECONDS = 30
MIN_KEEP_RATIO = 0.5  # an upstream list under half the previous size is a truncation, not news
ROOT = Path(__file__).resolve().parent.parent

JsonObj = dict[str, Any]

_APPID = re.compile(r"\d{1,10}")
_FIX_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._()+-]{0,127}")
_CREATED = re.compile(r"\d{4}-\d{2}-\d{2}(T[0-9:.]+(Z|[+-]\d\d:\d\d)?)?")
_GENERATED = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
# Query-parameter NAMES that mark a signed or credentialed URL. Such a URL must never reach a public file.
_SECRET_PARAM = re.compile(
    r"signature|x-amz|x-goog|token|expires|secret|password|api[_-]?key|access[_-]?key|auth|sig",
    re.IGNORECASE,
)
_SLOT_EXTENSIONS = {"manifest": (".zip", ".lua"), "fix": (".zip",)}
_SOURCES = ("lua.tools", "orphan", "merged")  # merged = in lua.tools AND orphaned_fixes.json


class CatalogError(Exception):
    """Anything that must stop the sync. The message names the offending entry."""


def _need(condition: bool, message: str) -> None:
    if not condition:
        raise CatalogError(message)


def _only_keys(raw: JsonObj, allowed: set[str], where: str) -> None:
    extra = set(raw) - allowed
    _need(not extra, f"{where}: unknown field(s) {sorted(extra)} (typo?)")


# ── URLs ─────────────────────────────────────────────────────────────────────


def _check_url(url: Any, where: str) -> str:
    """https only, a real public host name, no credentials, no signature-style query parameters."""
    _need(isinstance(url, str) and bool(url), f"{where}: url must be a non-empty string")
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    _need(parts.scheme == "https" and bool(host), f"{where}: url must be https with a host")
    _need(not (parts.username or parts.password or parts.fragment), f"{where}: url carries credentials or a fragment")
    try:
        ipaddress.ip_address(host)
        is_ip = True
    except ValueError:
        is_ip = False
    _need(
        not is_ip and "." in host and host != "localhost" and not host.endswith((".local", ".localhost")),
        f"{where}: host '{host}' is an IP literal or a local name",
    )
    for name, _ in urllib.parse.parse_qsl(parts.query, keep_blank_values=True):
        _need(not _SECRET_PARAM.search(name), f"{where}: query parameter '{name}' looks like a signed/credentialed url")
    return url


# ── Download + upstream parsing ─────────────────────────────────────────────


def fetch_upstream(url: str = UPSTREAM_URL) -> bytes:
    _check_url(url, "upstream")
    request = urllib.request.Request(  # noqa: S310 - scheme checked above
        url, headers={"Accept": "application/json", "User-Agent": "LuaTools-Catalog-Sync/1"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:  # noqa: S310
            _need(
                urllib.parse.urlsplit(response.geturl()).hostname == UPSTREAM_HOST,
                "download was redirected away from lua.tools",
            )
            body = response.read(MAX_BYTES + 1)
    except (OSError, http.client.HTTPException) as exc:
        raise CatalogError(f"download failed: {type(exc).__name__}: {exc}") from exc
    _need(len(body) <= MAX_BYTES, f"download larger than {MAX_BYTES} bytes")
    return body


def _tag(raw: Any) -> JsonObj:
    return {"id": raw["id"], "name": raw["name"], "slug": raw["slug"], "color": raw.get("color")}


def parse_upstream(body: bytes) -> tuple[list[JsonObj], list[JsonObj]]:
    """Whitelist-copy the listing. Fields we do not know are dropped, never forwarded."""
    try:
        doc = json.loads(body.decode("utf-8"))
        _need(isinstance(doc, dict), "upstream: top level is not an object")
        _need(isinstance(doc["games"], list) and bool(doc["games"]), "upstream: no games")
        _need(isinstance(doc["tags"], list), "upstream: tags is not a list")
        tags: dict[str, JsonObj] = {}
        for raw in doc["tags"]:
            tags.setdefault(raw["id"], _tag(raw))
        games: list[JsonObj] = []
        for raw in doc["games"]:
            game_tags = [_tag(t) for t in raw["tags"]]
            for tag in game_tags:  # a tag used by a game but missing from the catalogue still has to exist
                tags.setdefault(tag["id"], tag)
            games.append(
                {
                    "appid": raw["appid"],
                    "name": raw["name"],
                    "header_image": raw.get("header_image"),
                    "fixCount": raw["fixCount"],
                    "tags": game_tags,
                    "source": "lua.tools",
                }
            )
    except CatalogError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError) as exc:  # UnicodeDecodeError is a ValueError
        raise CatalogError(f"upstream: unexpected shape ({type(exc).__name__}: {exc})") from exc
    return games, list(tags.values())


# ── Orphaned fixes (the override file) ──────────────────────────────────────


def load_orphans(path: Path) -> JsonObj:
    try:
        doc = json.loads(path.read_text(encoding="utf-8-sig"))  # utf-8-sig: PowerShell 5.1 writes a BOM
    except (OSError, ValueError) as exc:
        raise CatalogError(f"{path.name}: cannot read ({type(exc).__name__}: {exc})") from exc
    _need(isinstance(doc, dict), f"{path.name}: top level is not an object")
    _only_keys(doc, {"schema_version", "tags", "games"}, path.name)
    _need(doc.get("schema_version") == SCHEMA_VERSION, f"{path.name}: schema_version must be {SCHEMA_VERSION}")
    _need(isinstance(doc.get("games"), list), f"{path.name}: games must be a list")
    return doc


def _resolve_tags(slugs: Any, by_slug: dict[str, JsonObj], where: str) -> list[JsonObj]:
    _need(isinstance(slugs, list), f"{where}: tags must be a list of tag slugs")
    out: list[JsonObj] = []
    for slug in slugs:
        _need(isinstance(slug, str) and slug in by_slug, f"{where}: unknown tag slug {slug!r}")
        out.append(by_slug[slug])
    return out


def _orphan_fix(raw: Any, by_slug: dict[str, JsonObj], where: str) -> JsonObj:
    _need(isinstance(raw, dict), f"{where}: must be an object")
    _only_keys(raw, {"id", "title", "description", "tags", "created_at", "manifest", "fix"}, where)
    downloads: JsonObj = {}
    filenames: dict[str, Any] = {}
    for slot in ("manifest", "fix"):
        if (item := raw.get(slot)) is None:
            continue
        _need(isinstance(item, dict), f"{where}.{slot}: must be an object")
        _only_keys(item, {"url", "sha256", "filename"}, f"{where}.{slot}")
        sha256 = item.get("sha256")  # Get-FileHash prints uppercase; the schema stores lowercase
        downloads[slot] = {"url": item.get("url"), "sha256": sha256.lower() if isinstance(sha256, str) else sha256}
        filenames[slot] = item.get("filename")
    return {
        "id": raw.get("id"),
        "title": raw.get("title"),
        "description": raw.get("description"),
        "tags": _resolve_tags(raw.get("tags", []), by_slug, where),
        "hasManifest": "manifest" in downloads,
        "hasFix": "fix" in downloads,
        "manifestFilename": filenames.get("manifest"),
        "fixFilename": filenames.get("fix"),
        "createdAt": raw.get("created_at"),
        "downloads": downloads,
    }


def _orphan_game(raw: Any, by_slug: dict[str, JsonObj], where: str) -> JsonObj:
    _need(isinstance(raw, dict), f"{where}: must be an object")
    _only_keys(raw, {"appid", "name", "header_image", "tags", "fixes"}, where)
    raw_fixes = raw.get("fixes")
    _need(isinstance(raw_fixes, list) and bool(raw_fixes), f"{where}: fixes must be a non-empty list")
    fixes = [_orphan_fix(f, by_slug, f"{where}.fixes[{i}]") for i, f in enumerate(raw_fixes)]
    tags: dict[str, JsonObj] = {}
    for tag in _resolve_tags(raw.get("tags", []), by_slug, where) + [t for f in fixes for t in f["tags"]]:
        tags.setdefault(tag["id"], tag)
    return {
        "appid": raw.get("appid"),
        "name": raw.get("name"),
        "header_image": raw.get("header_image"),
        "fixCount": len(fixes),
        "tags": list(tags.values()),
        "source": "orphan",
        "fixes": fixes,
    }


# ── Merge + final validation ─────────────────────────────────────────────────


def _combine(official: JsonObj, orphan: JsonObj) -> JsonObj:
    """One game in both sources: the official entry's identity, the union of fixes and tags.

    Fixes are concatenated official-first and de-duplicated by id (the first one wins), so an orphan fix can
    add to what lua.tools has but never replace it. The listing carries only a fixCount, no fix ids, so the
    count grows by the orphan fixes that were not already there; ids of the official fixes are only known to
    the GUI, which repeats this de-duplication when it lists a game's fixes.
    """
    known = list(official.get("fixes", []))
    fixes: dict[Any, JsonObj] = {}
    for fix in [*known, *orphan["fixes"]]:
        fixes.setdefault(fix["id"], fix)  # first wins: official before orphan
    tags = {t["id"]: t for t in [*official["tags"], *orphan["tags"]]}
    return {
        "appid": official["appid"],
        "name": official["name"],
        "header_image": official["header_image"],
        "fixCount": official["fixCount"] + len(fixes) - len(known),
        "tags": list(tags.values()),
        "source": "merged",
        "fixes": list(fixes.values()),
    }


def merge(upstream_games: list[JsonObj], upstream_tags: list[JsonObj], orphans: JsonObj) -> JsonObj:
    """lua.tools + orphans. A game in both is merged (see _combine); a game only in orphans is added."""
    by_slug = {t["slug"]: t for t in upstream_tags}
    tags = list(upstream_tags)
    for i, raw in enumerate(orphans.get("tags", [])):
        where = f"orphans.tags[{i}]"
        _need(isinstance(raw, dict), f"{where}: must be an object")
        _only_keys(raw, {"slug", "name", "color"}, where)
        slug = raw.get("slug")
        _need(isinstance(slug, str) and bool(slug), f"{where}: slug is required")
        if slug not in by_slug:  # an upstream tag with this slug wins; ours would only duplicate it
            tag = {"id": f"local-{slug}", "name": raw.get("name"), "slug": slug, "color": raw.get("color")}
            by_slug[slug] = tag
            tags.append(tag)

    by_appid = {g["appid"]: g for g in upstream_games}
    seen: set[Any] = set()
    for i, raw in enumerate(orphans["games"]):
        game = _orphan_game(raw, by_slug, f"orphans.games[{i}]")
        _need(game["appid"] not in seen, f"orphans.games[{i}]: appid {game['appid']!r} appears twice")
        seen.add(game["appid"])
        official = by_appid.get(game["appid"])
        by_appid[game["appid"]] = game if official is None else _combine(official, game)
    # (length, text) sorts digit strings numerically without trusting the appid to be a number yet.
    games = sorted(by_appid.values(), key=lambda g: (len(str(g["appid"])), str(g["appid"])))
    return {"schema_version": SCHEMA_VERSION, "tags": tags, "games": games}


def _validate_tag(tag: Any, where: str) -> None:
    _need(isinstance(tag, dict) and set(tag) == {"id", "name", "slug", "color"}, f"{where}: bad tag shape")
    for key in ("id", "name", "slug"):
        _need(isinstance(tag[key], str) and bool(tag[key]), f"{where}.{key}: must be a non-empty string")
    _need(tag["color"] is None or isinstance(tag["color"], str), f"{where}.color: must be a string or null")


def _validate_download(slot: str, item: Any, filename: Any, where: str) -> None:
    _need(isinstance(item, dict) and set(item) == {"url", "sha256"}, f"{where}.{slot}: bad download shape")
    _check_url(item["url"], f"{where}.{slot}.url")
    _need(
        isinstance(item["sha256"], str) and bool(_SHA256.fullmatch(item["sha256"])),
        f"{where}.{slot}.sha256: required, 64 lowercase hex characters",
    )
    _need(
        isinstance(filename, str)
        and bool(_FILENAME.fullmatch(filename))
        and filename.lower().endswith(_SLOT_EXTENSIONS[slot]),
        f"{where}.{slot}.filename: must be a plain file name ending in {_SLOT_EXTENSIONS[slot]}",
    )


def _validate_fix(fix: Any, tag_ids: set[str], where: str) -> None:
    keys = {
        "id",
        "title",
        "description",
        "tags",
        "hasManifest",
        "hasFix",
        "manifestFilename",
        "fixFilename",
        "createdAt",
        "downloads",
    }
    _need(isinstance(fix, dict) and set(fix) == keys, f"{where}: bad fix shape")
    _need(isinstance(fix["id"], str) and bool(_FIX_ID.fullmatch(fix["id"])), f"{where}.id: lowercase slug expected")
    _need(isinstance(fix["title"], str) and bool(fix["title"].strip()), f"{where}.title: required")
    _need(fix["description"] is None or isinstance(fix["description"], str), f"{where}.description: string or null")
    _need(
        fix["createdAt"] is None or (isinstance(fix["createdAt"], str) and bool(_CREATED.fullmatch(fix["createdAt"]))),
        f"{where}.created_at: ISO date expected",
    )
    _need(isinstance(fix["tags"], list), f"{where}.tags: list expected")
    for tag in fix["tags"]:
        _validate_tag(tag, f"{where}.tags")
        _need(tag["id"] in tag_ids, f"{where}: tag {tag['slug']!r} is not in the catalogue")
    downloads = fix["downloads"]
    _need(
        isinstance(downloads, dict) and bool(downloads) and set(downloads) <= {"manifest", "fix"},
        f"{where}: needs a manifest and/or fix download",
    )
    _need(
        fix["hasManifest"] is ("manifest" in downloads) and fix["hasFix"] is ("fix" in downloads),
        f"{where}: has* flags disagree with downloads",
    )
    for slot, filename in (("manifest", fix["manifestFilename"]), ("fix", fix["fixFilename"])):
        if slot in downloads:
            _validate_download(slot, downloads[slot], filename, where)
        else:
            _need(filename is None, f"{where}.{slot}: filename without a download")


def _validate_game(game: Any, tag_ids: set[str], where: str) -> None:
    base = {"appid", "name", "header_image", "fixCount", "tags", "source"}
    _need(isinstance(game, dict) and set(game) in (base, base | {"fixes"}), f"{where}: bad game shape")
    _need(isinstance(game["appid"], str) and bool(_APPID.fullmatch(game["appid"])), f"{where}.appid: digits expected")
    where = f"{where}[{game['appid']}]"
    _need(isinstance(game["name"], str) and bool(game["name"].strip()), f"{where}.name: required")
    if game["header_image"] is not None:
        _check_url(game["header_image"], f"{where}.header_image")
    _need(
        isinstance(game["fixCount"], int) and not isinstance(game["fixCount"], bool) and game["fixCount"] >= 0,
        f"{where}.fixCount: non-negative integer expected",
    )
    _need(isinstance(game["tags"], list), f"{where}.tags: list expected")
    for tag in game["tags"]:
        _validate_tag(tag, f"{where}.tags")
        _need(tag["id"] in tag_ids, f"{where}: tag {tag['slug']!r} is not in the catalogue")
    _need(game["source"] in _SOURCES, f"{where}.source: one of {_SOURCES}")
    if game["source"] in ("orphan", "merged"):
        fixes = game.get("fixes")
        _need(isinstance(fixes, list) and bool(fixes), f"{where}: fixes must be a non-empty list")
        if game["source"] == "orphan":
            _need(game["fixCount"] == len(fixes), f"{where}: fixCount must equal len(fixes)")
        else:  # the official fixes are counted but not listed
            _need(game["fixCount"] >= len(fixes), f"{where}: fixCount must be at least len(fixes)")
        ids = [f.get("id") if isinstance(f, dict) else None for f in fixes]
        _need(len(set(ids)) == len(ids), f"{where}: duplicate fix id")
        for i, fix in enumerate(fixes):
            _validate_fix(fix, tag_ids, f"{where}.fixes[{i}]")
    else:
        _need("fixes" not in game, f"{where}: lua.tools entries carry no fixes[]")


def validate_catalog(doc: Any) -> None:
    """The schema, in code. Runs on every merge result before anything is written."""
    _need(isinstance(doc, dict), "catalog: not an object")
    _need(
        set(doc) in ({"schema_version", "tags", "games"}, {"schema_version", "generated_at", "tags", "games"}),
        "catalog: bad top-level keys",
    )
    _need(doc["schema_version"] == SCHEMA_VERSION, f"catalog: schema_version must be {SCHEMA_VERSION}")
    if "generated_at" in doc:
        _need(
            isinstance(doc["generated_at"], str) and bool(_GENERATED.fullmatch(doc["generated_at"])),
            "catalog: bad generated_at",
        )
    _need(
        isinstance(doc["tags"], list) and isinstance(doc["games"], list) and bool(doc["games"]),
        "catalog: tags/games missing or empty",
    )
    for i, tag in enumerate(doc["tags"]):
        _validate_tag(tag, f"tags[{i}]")
    tag_ids = {t["id"] for t in doc["tags"]}
    _need(
        len(tag_ids) == len(doc["tags"]) and len({t["slug"] for t in doc["tags"]}) == len(doc["tags"]),
        "tags: duplicate id or slug",
    )
    for i, game in enumerate(doc["games"]):
        _validate_game(game, tag_ids, f"games[{i}]")
    appids = [g["appid"] for g in doc["games"]]
    _need(len(set(appids)) == len(appids), "games: duplicate appid")


# ── Run ──────────────────────────────────────────────────────────────────────


def _read_previous(path: Path) -> JsonObj | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _upstream_count(previous: JsonObj | None) -> int:
    try:
        return sum(1 for g in (previous or {}).get("games", []) if g.get("source") in ("lua.tools", "merged"))
    except (TypeError, AttributeError):
        return 0  # a damaged previous file cannot veto the next sync


def _write_atomic(path: Path, doc: JsonObj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def run(
    catalog: Path,
    orphans: Path,
    fetch: Callable[[], bytes] = fetch_upstream,
    now: Callable[[], datetime] = _utc_now,
) -> int:
    """0 = written or unchanged. 1 = aborted; catalog.json untouched."""
    previous = _read_previous(catalog)
    try:
        up_games, up_tags = parse_upstream(fetch())
        kept = _upstream_count(previous)
        _need(
            len(up_games) >= kept * MIN_KEEP_RATIO,
            f"upstream shrank from {kept} to {len(up_games)} games; refusing to publish it",
        )
        doc = merge(up_games, up_tags, load_orphans(orphans))
        validate_catalog(doc)
    except CatalogError as exc:
        print(f"ABORT (catalog.json untouched): {exc}", file=sys.stderr)
        return 1

    added = sorted((g["appid"] for g in doc["games"] if g["source"] == "orphan"), key=int)
    merged = sorted((g["appid"] for g in doc["games"] if g["source"] == "merged"), key=int)
    print(f"lua.tools: {len(up_games)} games, orphans added: {added}, merged into lua.tools games: {merged}")

    if previous is not None and {k: v for k, v in previous.items() if k != "generated_at"} == doc:
        print("catalog.json unchanged")
        return 0
    final = {
        "schema_version": doc["schema_version"],
        "generated_at": now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tags": doc["tags"],
        "games": doc["games"],
    }
    validate_catalog(final)
    _write_atomic(catalog, final)
    print(f"catalog.json written ({len(final['games'])} games)")
    return 0


def _as_official(game: JsonObj) -> JsonObj:
    """Undo _combine on a previous catalog entry, so --check can merge the (possibly edited) orphans again."""
    if game["source"] != "merged":
        return game
    return {
        **{k: v for k, v in game.items() if k != "fixes"},
        "fixCount": game["fixCount"] - len(game["fixes"]),
        "tags": [t for t in game["tags"] if not t["id"].startswith("local-")],
        "source": "lua.tools",
    }


def check(catalog: Path, orphans: Path) -> int:
    """Offline: merge orphaned_fixes.json into the lua.tools part of the current catalog.json."""
    previous = _read_previous(catalog)
    if previous is None:
        print(f"CHECK FAILED: {catalog.name} is needed for --check and is not readable", file=sys.stderr)
        return 1
    try:
        up_games = [_as_official(g) for g in previous["games"] if g["source"] != "orphan"]
        up_tags = [t for t in previous["tags"] if not t["id"].startswith("local-")]
        validate_catalog(merge(up_games, up_tags, load_orphans(orphans)))
    except (CatalogError, KeyError, TypeError) as exc:
        print(f"CHECK FAILED: {exc}", file=sys.stderr)
        return 1
    print("orphaned_fixes.json OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--catalog", type=Path, default=ROOT / "catalog.json")
    parser.add_argument("--orphans", type=Path, default=ROOT / "orphaned_fixes.json")
    parser.add_argument("--check", action="store_true", help="validate the override file offline, no download")
    args = parser.parse_args(argv)
    return check(args.catalog, args.orphans) if args.check else run(args.catalog, args.orphans)


if __name__ == "__main__":
    sys.exit(main())
