#!/usr/bin/env python3
"""
plex-playlist-sync
-------------------
Keeps one or more playlists owned by the Plex server admin in two-way sync
with one or more Plex Home / managed users on the same server.

Plex has no native "live sync" for playlists across Home users — the built-in
Share option on a playlist only hands out a one-time, read-only snapshot that
does NOT update when the original is edited. This script closes that gap by
periodically merging the admin's source playlist with each target user's copy:
whichever item any side has, every side ends up with.

Configuration is via environment variables (see .env.example):
  PLEX_URL             e.g. http://192.168.1.50:32400
  PLEX_TOKEN            admin/owner X-Plex-Token (required to switch users)
  PLAYLIST_NAMES        comma-separated, exact, case-sensitive playlist titles
  TARGET_USERS          comma-separated Plex Home usernames, e.g. "Kate"
  SYNC_INTERVAL_HOURS   how often to re-sync (default 24)

Every playlist in PLAYLIST_NAMES is synced to every user in TARGET_USERS.

How the merge works ("addition wins"):
  * Each run compares the *current* contents of the source playlist and each
    target user's copy — there's no history of past syncs kept anywhere.
  * An item present on either side is added to whichever side is missing it.
  * Nothing is ever deleted by this script. The only way an item stops being
    on both copies is if it's removed from *both* sides (independently, by
    whoever edits them) before the next sync runs.
  * Practically: if you remove an item from the source but a target user
    still has it, it comes back into your source playlist on the next sync
    (their copy "wins"). Likewise, anything you keep in the source that a
    user removed from their copy is added back to theirs.

Notes / limitations:
  * Smart playlists (rule-based, not a fixed item list) are skipped — there's
    nothing to "copy" since they're generated on the fly. Convert to a
    regular playlist first if you want it synced this way.
  * With more than one user in TARGET_USERS for the same playlist, this is a
    hub-and-spoke merge through the source: an item any one user adds is
    merged into the source playlist first, and from there reaches every
    other target user on the same or a later run — not just the user who
    added it.
  * New items are appended to the end of each playlist — existing item order
    is never changed, so playlists can drift out of sync with each other's
    ordering over time.
  * Requires the SERVER ADMIN token, because switching into another user's
    context (switchUser) is an admin-only operation.
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone

from plexapi.exceptions import NotFound
from plexapi.server import PlexServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("playlist-sync")


def get_config():
    url = os.environ.get("PLEX_URL")
    token = os.environ.get("PLEX_TOKEN")
    playlist_names_raw = os.environ.get("PLAYLIST_NAMES", "")
    target_users_raw = os.environ.get("TARGET_USERS", "")
    interval_hours = float(os.environ.get("SYNC_INTERVAL_HOURS", "24"))

    missing = [
        name
        for name, value in [
            ("PLEX_URL", url),
            ("PLEX_TOKEN", token),
        ]
        if not value
    ]
    if missing:
        log.error("Missing required environment variable(s): %s", ", ".join(missing))
        sys.exit(1)

    playlist_names = [p.strip() for p in playlist_names_raw.split(",") if p.strip()]
    if not playlist_names:
        log.error("PLAYLIST_NAMES is empty — set at least one playlist title.")
        sys.exit(1)

    target_users = [u.strip() for u in target_users_raw.split(",") if u.strip()]
    if not target_users:
        log.error("TARGET_USERS is empty — set at least one Plex Home username.")
        sys.exit(1)

    return url, token, playlist_names, target_users, interval_hours


def fetch_items(server: PlexServer, keys: set[int], owner_label: str):
    """Fetch items by ratingKey through `server`, skipping any that fail
    (e.g. the underlying media was deleted from the library)."""
    items = []
    for key in keys:
        try:
            items.append(server.fetchItem(key))
        except Exception as exc:  # noqa: BLE001 - one bad item shouldn't break the sync
            log.warning("Could not fetch item %s for %s: %s", key, owner_label, exc)
    return items


def sync_once(url: str, token: str, playlist_name: str, target_users: list[str]) -> None:
    admin = PlexServer(url, token)

    try:
        source_playlist = admin.playlist(playlist_name)
    except NotFound:
        log.error("Playlist %r not found on the server (check spelling/case).", playlist_name)
        return

    if source_playlist.smart:
        log.warning(
            "Playlist %r is a Smart Playlist. Smart playlists are rule-based and "
            "can't be merged item-by-item — skipping. Convert it to a regular "
            "playlist if you want it synced.",
            playlist_name,
        )
        return

    source_keys = {item.ratingKey for item in source_playlist.items()}

    # Pass 1: look up each target user's current copy, and collect any items
    # they have that the source doesn't (yet).
    user_state = {}
    keys_missing_from_source: set[int] = set()

    for username in target_users:
        try:
            user_server = admin.switchUser(username)
        except NotFound:
            log.error("Home user %r not found (check the exact Plex username).", username)
            continue
        except Exception as exc:  # noqa: BLE001 - keep the loop going for other users
            log.error("Could not switch to user %r: %s", username, exc)
            continue

        try:
            existing = user_server.playlist(playlist_name)
            existing_keys = {item.ratingKey for item in existing.items()}
        except NotFound:
            existing = None
            existing_keys = set()
        except Exception as exc:  # noqa: BLE001
            log.error("Could not look up %r for %s: %s", playlist_name, username, exc)
            continue

        user_state[username] = {"server": user_server, "playlist": existing, "keys": existing_keys}
        keys_missing_from_source |= existing_keys - source_keys

    # Pass 2: merge any user-only items into the source playlist.
    if keys_missing_from_source:
        new_source_items = fetch_items(admin, keys_missing_from_source, "source playlist")
        if new_source_items:
            try:
                source_playlist.addItems(new_source_items)
                source_keys |= {item.ratingKey for item in new_source_items}
                log.info(
                    "Pulled %d item(s) into source playlist %r from target users' copies.",
                    len(new_source_items),
                    playlist_name,
                )
            except Exception as exc:  # noqa: BLE001
                log.error("Failed to merge user items into source playlist %r: %s", playlist_name, exc)

    if not source_keys:
        log.warning("Playlist %r has no items on any side — nothing to sync.", playlist_name)
        return

    # Pass 3: push whatever's missing on each target user's side.
    for username, state in user_state.items():
        user_server = state["server"]
        existing = state["playlist"]
        missing_for_user = source_keys - state["keys"]

        if existing is None:
            new_items = fetch_items(user_server, missing_for_user, username)
            if not new_items:
                log.warning("Nothing to create %r for %s.", playlist_name, username)
                continue
            try:
                user_server.createPlaylist(playlist_name, items=new_items)
                log.info("Created %r for %s (%d item(s)).", playlist_name, username, len(new_items))
            except Exception as exc:  # noqa: BLE001
                log.error("Failed to create playlist for %s: %s", username, exc)
            continue

        if not missing_for_user:
            log.info("%s's copy of %r is already up to date (%d item(s)).", username, playlist_name, len(state["keys"]))
            continue

        new_items = fetch_items(user_server, missing_for_user, username)
        if not new_items:
            continue
        try:
            existing.addItems(new_items)
            log.info(
                "Synced %r for %s: added %d item(s) (%d total).",
                playlist_name,
                username,
                len(new_items),
                len(state["keys"]) + len(new_items),
            )
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to update playlist for %s: %s", username, exc)


def main() -> None:
    url, token, playlist_names, target_users, interval_hours = get_config()
    log.info(
        "Starting two-way playlist sync: %s <-> %s, every %s hour(s).",
        ", ".join(repr(p) for p in playlist_names),
        ", ".join(target_users),
        interval_hours,
    )

    while True:
        start = datetime.now(timezone.utc)
        for playlist_name in playlist_names:
            try:
                sync_once(url, token, playlist_name, target_users)
            except Exception:  # noqa: BLE001 - never let one bad run kill the container
                log.exception("Unexpected error syncing %r.", playlist_name)
        log.info(
            "Run finished (started %s). Sleeping %s hour(s) until the next sync.",
            start.isoformat(timespec="seconds"),
            interval_hours,
        )
        time.sleep(max(interval_hours, 0.05) * 3600)


if __name__ == "__main__":
    main()
