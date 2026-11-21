# LuaTools-Catalog

Consolidated Fixes catalog for LuaTools Amethyst: the public `lua.tools` listing **plus** a hand-curated
override file for games that catalog does not cover ("orphaned fixes"). A GitHub Action refreshes
`catalog.json` every 6 hours. Standard-library Python, no secrets.

| File | Role |
|---|---|
| `catalog.json` | Output. Generated; do not edit by hand. |
| `orphaned_fixes.json` | Input. The override: games/fixes `lua.tools` does not have. Edit this one. |
| `scripts/sync_catalog.py` | Download + merge + validate + write. |
| `.github/workflows/sync.yml` | Every 6 h: tests, sync, commit `catalog.json` if it changed. |
| `.github/workflows/check.yml` | On PRs/pushes: tests + offline validation of `orphaned_fixes.json`. |

## `catalog.json` (schema_version 1)

A **superset of what `/api/denuvo/listings` returns today**: `games[]` and `tags[]` keep the same property
names, so the GUI's existing `DenuvoListingsResponse` reads it unchanged (extra fields are ignored).

```jsonc
{
  "schema_version": 1,
  "generated_at": "2026-11-20T18:07:25Z",      // only changes when the content changes
  "tags": [ { "id": "…", "name": "Online Fix", "slug": "online-fix", "color": "#3b82f6" } ],
  "games": [                                    // sorted by appid
    {
      "appid": "70",                            // string of digits
      "name": "Half-Life",
      "header_image": "https://…/header.jpg",   // https or null
      "fixCount": 1,
      "tags": [ /* full tag objects, all present in tags[] */ ],
      "source": "lua.tools"                     // or "orphan" (only in orphaned_fixes.json) or "merged" (in both)
      // "orphan" and "merged" entries also carry:
      // "fixes": [ {
      //   "id": "online-1", "title": "…", "description": null,
      //   "tags": [ /* tag objects */ ],
      //   "hasManifest": false, "hasFix": true,
      //   "manifestFilename": null, "fixFilename": "fix.zip",
      //   "createdAt": "2026-11-20",
      //   "downloads": { "fix": { "url": "https://…", "sha256": "<64 lowercase hex>" } }
      // } ]
    }
  ]
}
```

`lua.tools` entries carry **no** download links: their fixes are still listed per game by
`/api/denuvo/fixes` and downloaded through the signed, authenticated `/api/denuvo/download`. Only the fixes in
`orphan`/`merged` entries have `downloads`, and those are plain public https links, each pinned by a SHA-256.

## `orphaned_fixes.json` (what you edit)

```jsonc
{
  "schema_version": 1,
  "tags": [ { "slug": "community", "name": "Community", "color": "#22c55e" } ],   // optional new tags
  "games": [
    {
      "appid": "123456",
      "name": "Some Game",
      "header_image": "https://shared.akamai.steamstatic.com/…/header.jpg",       // or null
      "tags": ["online-fix"],                                                       // slugs; optional
      "fixes": [
        {
          "id": "online-1",                       // lowercase slug, unique within the game
          "title": "Online fix",
          "description": "optional text",
          "tags": ["online-fix", "community"],    // slugs from lua.tools or defined above
          "created_at": "2026-11-20",
          "manifest": { "url": "https://…/manifest.zip", "sha256": "…", "filename": "manifest.zip" },   // optional
          "fix":      { "url": "https://…/fix.zip",      "sha256": "…", "filename": "fix.zip" }        // optional
        }
      ]
    }
  ]
}
```

At least one of `manifest` / `fix` per fix. `manifest` files end in `.zip` or `.lua`; `fix` files in `.zip`.
Unknown fields are rejected on purpose (a typo such as `sha26` must not slip through).

**Same `appid` in both:** the two are **merged**, never replaced. The game keeps the `lua.tools` name, image and
tags (plus yours), `source` becomes `"merged"`, `fixes[]` lists only your fixes and `fixCount` is the official
count plus yours. The `lua.tools` listing carries no fix ids, so the catalog cannot de-duplicate against the
official fixes; the GUI does it when it lists a game's fixes (official first, a repeated id is dropped). Within
the merge itself, fixes are concatenated official-first and a repeated id keeps the first.

## Rules the script enforces (fail-closed)

The run aborts with exit code 1 and leaves `catalog.json` byte-for-byte as it was when:

- the download fails, is redirected off `lua.tools`, exceeds 8 MB, or is not the expected JSON shape;
- the listing is empty, or has fewer than half the games the previous catalog had (a truncation);
- `orphaned_fixes.json` is missing, malformed, has unknown fields, an unknown tag slug or a repeated appid;
- the merged result fails `validate_catalog` (the schema, in code).

Nothing unknown from upstream is forwarded: games are rebuilt from a whitelist of fields. Every URL in the
output must be `https`, a real public host (no IP literal, `localhost`, single-label or `.local` name), with
no credentials and **no query parameter that looks signed** (`signature`, `x-amz-*`, `token`, `expires`, `key`,
`auth`, …). Orphan downloads require a SHA-256 (64 hex characters, any case; stored lowercase), so the consumer can verify before applying.

## Local use

```powershell
py -m pip install pytest
py -m pytest -q
py scripts/sync_catalog.py            # real download + write
py scripts/sync_catalog.py --check    # offline: validate orphaned_fixes.json
```

## Publishing this repository (needs your GitHub account)

1. On GitHub create an **empty** repository named `LuaTools-Catalog` (no README, no .gitignore, no licence).
2. From this folder: `git remote add origin https://github.com/<you>/LuaTools-Catalog.git` then `git push -u origin main`.
3. *Settings → Actions → General → Workflow permissions:* **Read and write permissions** (the sync commits
   `catalog.json` with the job's own `GITHUB_TOKEN`; no personal token is needed).
4. *Actions → sync-catalog → Run workflow* once to confirm it is green. After that it runs every 6 hours.
5. The raw file the GUI will eventually read is
   `https://raw.githubusercontent.com/<you>/LuaTools-Catalog/main/catalog.json` (a host that is **not** yet on
   the app's DoH allowlist or network checks; that is a GUI-side change for a later phase).
