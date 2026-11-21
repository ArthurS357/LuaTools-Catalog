"""sync_catalog.py: override priority, fail-safe on a failed download, and a schema-valid result."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sync_catalog as sc

SHA = "a" * 64
TAGS = [
    {"id": "t-generic", "name": "Generic", "slug": "generic", "color": "#ec4899"},
    {"id": "t-online", "name": "Online Fix", "slug": "online-fix", "color": "#3b82f6"},
]
FIXED_NOW = datetime(2026, 11, 20, 12, 0, 0, tzinfo=UTC)


def upstream_body(*games: dict[str, Any], tags: list[dict[str, Any]] | None = None) -> bytes:
    listing = {"games": list(games), "tags": TAGS if tags is None else tags}
    return json.dumps(listing).encode("utf-8")


def upstream_game(appid: str, name: str, **extra: Any) -> dict[str, Any]:
    return {
        "appid": appid,
        "name": name,
        "header_image": f"https://cdn.example.com/{appid}/header.jpg?t=1779899040",
        "fixCount": 2,
        "tags": [TAGS[0]],
        **extra,
    }


def orphan_game(appid: str = "200", name: str = "Orphan Game", **fix_extra: Any) -> dict[str, Any]:
    return {
        "appid": appid,
        "name": name,
        "header_image": None,
        "fixes": [
            {
                "id": "online-1",
                "title": "Online fix",
                "tags": ["online-fix"],
                "created_at": "2026-11-20",
                "fix": {"url": "https://example.com/files/fix.zip", "sha256": SHA, "filename": "fix.zip"},
                **fix_extra,
            }
        ],
    }


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    orphans = tmp_path / "orphaned_fixes.json"
    write_orphans(orphans, [])
    return tmp_path / "catalog.json", orphans


def write_orphans(path: Path, games: list[dict[str, Any]], tags: list[dict[str, Any]] | None = None) -> None:
    path.write_text(json.dumps({"schema_version": 1, "tags": tags or [], "games": games}), encoding="utf-8")


def sync(paths: tuple[Path, Path], body: bytes) -> int:
    return sc.run(paths[0], paths[1], fetch=lambda: body, now=lambda: FIXED_NOW)


def read(catalog: Path) -> dict[str, Any]:
    return json.loads(catalog.read_text(encoding="utf-8"))


def by_appid(doc: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {g["appid"]: g for g in doc["games"]}


# ── 1. override: add and overwrite ───────────────────────────────────────────


def test_orphan_game_is_added_next_to_the_lua_tools_games(paths: tuple[Path, Path]) -> None:
    write_orphans(paths[1], [orphan_game("200")])

    assert sync(paths, upstream_body(upstream_game("100", "Upstream"))) == 0

    games = by_appid(read(paths[0]))
    assert set(games) == {"100", "200"}
    assert games["100"]["source"] == "lua.tools"
    assert games["200"]["source"] == "orphan"
    assert games["200"]["fixCount"] == 1


def test_orphan_is_merged_into_the_lua_tools_entry_for_the_same_appid(paths: tuple[Path, Path]) -> None:
    """Was 'orphan replaces the entry' until the F2 phase 2 decision: an orphan must not hide official fixes."""
    write_orphans(paths[1], [orphan_game("100", "Orphan name")])

    assert sync(paths, upstream_body(upstream_game("100", "Upstream"), upstream_game("101", "Other"))) == 0

    games = by_appid(read(paths[0]))
    merged = games["100"]
    assert merged["source"] == "merged"
    assert merged["name"] == "Upstream"  # the official identity is kept
    assert merged["header_image"].startswith("https://cdn.example.com/100/")
    assert merged["fixCount"] == 2 + 1  # the 2 official fixes plus the orphan one
    assert [f["id"] for f in merged["fixes"]] == ["online-1"]  # only ours are listed; the GUI adds the official
    assert {t["slug"] for t in merged["tags"]} == {"generic", "online-fix"}
    assert games["101"]["name"] == "Other"  # untouched
    assert len(games) == 2


def test_combine_concatenates_fixes_official_first_and_drops_duplicate_ids() -> None:
    official = {
        "appid": "100", "name": "Official", "header_image": None, "fixCount": 2,
        "tags": [TAGS[0]], "source": "lua.tools",
        "fixes": [{"id": "a", "title": "official a"}, {"id": "b", "title": "official b"}],
    }  # fmt: skip
    orphan = {"fixes": [{"id": "b", "title": "orphan b"}, {"id": "c", "title": "orphan c"}], "tags": [TAGS[1]]}

    combined = sc._combine(official, orphan)

    assert [(f["id"], f["title"]) for f in combined["fixes"]] == [
        ("a", "official a"), ("b", "official b"), ("c", "orphan c"),
    ]  # fmt: skip
    assert combined["fixCount"] == 3  # 2 + only the new one
    assert combined["source"] == "merged"


def test_merged_entry_survives_a_check_run_and_a_second_sync_is_unchanged(paths: tuple[Path, Path]) -> None:
    write_orphans(paths[1], [orphan_game("100")])
    body = upstream_body(upstream_game("100", "Upstream"))
    assert sync(paths, body) == 0
    before = paths[0].read_bytes()

    assert sc.check(paths[0], paths[1]) == 0  # rebuilds the official entry from the merged one
    assert sync(paths, body) == 0
    assert paths[0].read_bytes() == before


def test_orphan_tags_resolve_against_upstream_and_new_local_tags_are_added(paths: tuple[Path, Path]) -> None:
    write_orphans(
        paths[1],
        [orphan_game("200", tags=["online-fix", "community"])],
        tags=[{"slug": "community", "name": "Community"}],
    )

    assert sync(paths, upstream_body(upstream_game("100", "Upstream"))) == 0

    doc = read(paths[0])
    assert {t["slug"] for t in doc["tags"]} == {"generic", "online-fix", "community"}
    fix_tags = by_appid(doc)["200"]["fixes"][0]["tags"]
    assert [t["slug"] for t in fix_tags] == ["online-fix", "community"]


# ── 2. fail-safe: nothing is published unless everything is good ─────────────


def failing_fetch() -> bytes:
    raise sc.CatalogError("download failed: URLError: no route to host")


def test_failed_download_keeps_the_previous_catalog_byte_for_byte(paths: tuple[Path, Path]) -> None:
    assert sync(paths, upstream_body(upstream_game("100", "Upstream"))) == 0
    before = paths[0].read_bytes()

    assert sc.run(paths[0], paths[1], fetch=failing_fetch, now=lambda: FIXED_NOW) == 1

    assert paths[0].read_bytes() == before


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"[]", b'{"games": [], "tags": []}', b'{"games": [{"appid": "1"}], "tags": []}', b"\xff\xfe"],
    ids=["empty", "garbage", "wrong-top-level", "no-games", "missing-fields", "not-utf8"],
)
def test_unparseable_or_empty_upstream_aborts_and_keeps_the_previous_catalog(
    paths: tuple[Path, Path], body: bytes
) -> None:
    assert sync(paths, upstream_body(upstream_game("100", "Upstream"))) == 0
    before = paths[0].read_bytes()

    assert sync(paths, body) == 1

    assert paths[0].read_bytes() == before


def test_no_catalog_is_created_when_the_first_download_fails(paths: tuple[Path, Path]) -> None:
    assert sc.run(paths[0], paths[1], fetch=failing_fetch) == 1
    assert not paths[0].exists()


def test_a_suspicious_shrink_is_refused(paths: tuple[Path, Path]) -> None:
    many = [upstream_game(str(i), f"Game {i}") for i in range(1, 11)]
    assert sync(paths, upstream_body(*many)) == 0
    before = paths[0].read_bytes()

    assert sync(paths, upstream_body(*many[:3])) == 1  # 10 -> 3

    assert paths[0].read_bytes() == before


def test_invalid_orphans_file_aborts_without_touching_the_catalog(paths: tuple[Path, Path]) -> None:
    assert sync(paths, upstream_body(upstream_game("100", "Upstream"))) == 0
    before = paths[0].read_bytes()
    paths[1].write_text("{ broken", encoding="utf-8")

    assert sync(paths, upstream_body(upstream_game("100", "Upstream"), upstream_game("101", "New"))) == 1

    assert paths[0].read_bytes() == before


# ── 3. the result is valid and follows the schema ────────────────────────────


def test_written_catalog_is_valid_json_and_passes_the_schema(paths: tuple[Path, Path]) -> None:
    write_orphans(paths[1], [orphan_game("200")])
    assert sync(paths, upstream_body(upstream_game("100", "Upstream"))) == 0

    doc = json.loads(paths[0].read_text(encoding="utf-8"))
    sc.validate_catalog(doc)
    assert doc["schema_version"] == 1
    assert doc["generated_at"] == "2026-11-20T12:00:00Z"
    assert list(by_appid(doc)["100"]) == ["appid", "name", "header_image", "fixCount", "tags", "source"]
    assert by_appid(doc)["200"]["fixes"][0]["downloads"] == {
        "fix": {"url": "https://example.com/files/fix.zip", "sha256": SHA}
    }


def test_catalog_is_a_superset_of_the_listing_the_gui_already_reads(paths: tuple[Path, Path]) -> None:
    assert sync(paths, upstream_body(upstream_game("100", "Upstream"))) == 0

    game = read(paths[0])["games"][0]
    assert {"appid", "name", "header_image", "fixCount", "tags"} <= set(game)  # DenuvoGameListing's properties


def test_games_are_sorted_by_appid_numerically(paths: tuple[Path, Path]) -> None:
    body = upstream_body(upstream_game("1000", "c"), upstream_game("20", "a"), upstream_game("300", "b"))
    assert sync(paths, body) == 0

    assert [g["appid"] for g in read(paths[0])["games"]] == ["20", "300", "1000"]


def test_unchanged_upstream_does_not_rewrite_the_file(paths: tuple[Path, Path]) -> None:
    body = upstream_body(upstream_game("100", "Upstream"))
    assert sync(paths, body) == 0
    before = paths[0].read_bytes()

    later = datetime(2026, 11, 21, tzinfo=UTC)
    assert sc.run(paths[0], paths[1], fetch=lambda: body, now=lambda: later) == 0

    assert paths[0].read_bytes() == before  # generated_at did not move either


# ── security: no signed URLs or tokens in the public file ────────────────────


def test_upstream_fields_we_do_not_know_are_never_forwarded(paths: tuple[Path, Path]) -> None:
    leaky = upstream_game(
        "100",
        "Upstream",
        download_url="https://r2.example.com/x?X-Amz-Signature=abc",
        token="s3cr3t",  # noqa: S106
    )
    assert sync(paths, upstream_body(leaky)) == 0

    text = paths[0].read_text(encoding="utf-8")
    assert "X-Amz" not in text
    assert "s3cr3t" not in text
    assert "download_url" not in text


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/fix.zip?X-Amz-Signature=abc",
        "https://example.com/fix.zip?token=abc",
        "https://example.com/fix.zip?Expires=1&Signature=2",
        "http://example.com/fix.zip",
        "https://user:pw@example.com/fix.zip",
        "https://127.0.0.1/fix.zip",
        "https://localhost/fix.zip",
        "https://intranet/fix.zip",
        "ftp://example.com/fix.zip",
    ],
)
def test_orphan_download_urls_must_be_plain_public_https(paths: tuple[Path, Path], url: str) -> None:
    game = orphan_game("200")
    game["fixes"][0]["fix"]["url"] = url
    write_orphans(paths[1], [game])

    assert sync(paths, upstream_body(upstream_game("100", "Upstream"))) == 1
    assert not paths[0].exists()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda g: g["fixes"][0]["fix"].pop("sha256"),
        lambda g: g["fixes"][0]["fix"].update(sha256="ABC"),
        lambda g: g["fixes"][0]["fix"].update(filename="../evil.zip"),
        lambda g: g["fixes"][0]["fix"].update(filename="fix.exe"),
        lambda g: g["fixes"][0].update(tags=["no-such-tag"]),
        lambda g: g["fixes"][0].update(sha26="typo"),
        lambda g: g.update(appid="12x"),
        lambda g: g.update(fixes=[]),
        lambda g: g["fixes"][0].pop("fix"),
        lambda g: g.update(header_image="http://example.com/h.jpg"),
    ],
    ids=[
        "no-sha",
        "short-sha",
        "path-filename",
        "exe",
        "unknown-tag",
        "typo-field",
        "bad-appid",
        "no-fixes",
        "no-download",
        "http-header",
    ],
)
def test_bad_orphan_entries_abort(paths: tuple[Path, Path], mutate: Any) -> None:
    game = copy.deepcopy(orphan_game("200"))
    mutate(game)
    write_orphans(paths[1], [game])

    assert sync(paths, upstream_body(upstream_game("100", "Upstream"))) == 1
    assert not paths[0].exists()


def test_uppercase_sha256_from_get_filehash_is_accepted_and_stored_lowercase(paths: tuple[Path, Path]) -> None:
    game = orphan_game("200")
    game["fixes"][0]["fix"]["sha256"] = "AB" * 32
    write_orphans(paths[1], [game])

    assert sync(paths, upstream_body(upstream_game("100", "Upstream"))) == 0

    assert by_appid(read(paths[0]))["200"]["fixes"][0]["downloads"]["fix"]["sha256"] == "ab" * 32


def test_the_same_appid_twice_in_the_orphans_file_aborts(paths: tuple[Path, Path]) -> None:
    write_orphans(paths[1], [orphan_game("200"), orphan_game("200", "Again")])

    assert sync(paths, upstream_body(upstream_game("100", "Upstream"))) == 1


def test_check_mode_validates_the_orphans_against_the_current_catalog(paths: tuple[Path, Path]) -> None:
    assert sync(paths, upstream_body(upstream_game("100", "Upstream"))) == 0
    write_orphans(paths[1], [orphan_game("200")])
    assert sc.check(paths[0], paths[1]) == 0

    bad = orphan_game("200")
    bad["fixes"][0]["fix"]["url"] = "http://example.com/fix.zip"
    write_orphans(paths[1], [bad])
    assert sc.check(paths[0], paths[1]) == 1
