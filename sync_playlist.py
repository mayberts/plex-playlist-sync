#!/usr/bin/env python3
"""
plex-playlist-sync
-------------------
Keeps a copy of one or more playlists owned by the Plex server admin in sync
with one or more Plex Home / managed users on the same server.

Plex has no native "live sync" for playlists across Home users — the built-in
Share option on a playlist only hands out a one-time, read-only snapshot that
does NOT update when the original is edited. This script works around that by
periodically deleting each target user's copy and recreating it from the
current contents of the source playlist.

Configuration is via environment variables (see .env.example):
  PLEX_URL             e.g. http://192.168.1.50:32400
  PLEX_TOKEN            admin/owner X-Plex-Token (required to switch users)
  PLAYLIST_NAMES        comma-separated, exact, case-sensitive playlist titles
  TARGET_USERS          comma-separated Plex Home usernames, e.g. "Kate"
  SYNC_INTERVAL_HOURS   how often to re-sync (default 24)

Every playlist in PLAYLIST_NAMES is synced to every user in TARGET_USERS.

Notes / limitations:
  * Smart playlists (rule-based, not a fixed item list) are skipped — there's
    nothing to "copy" since they're generated on the fly. Convert to a
    regular playlist first if you want it synced this way.
  * This is one-directional: Nick's edits flow to Kate. If Kate edits her
    copy, those edits are overwritten on the next sync run.
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


def sync_once(url: str, token: str, playlist_name: str, target_users: list[str]) -> None:
    admin = PlexServer(url, token)

    try:
        playlist = admin.playlist(playlist_name)
    except NotFound:
        log.error("Playlist %r not found on the server (check spelling/case).", playlist_name)
        return

    if playlist.smart:
        log.warning(
            "Playlist %r is a Smart Playlist. Smart playlists are rule-based and "
            "can't be copied item-by-item — skipping. Convert it to a regular "
            "playlist if you want it synced.",
            playlist_name,
        )
        return

    items = playlist.items()
    if not items:
        log.warning("Playlist %r has no items — nothing to sync.", playlist_name)
        return

    log.info("Source playlist %r has %d item(s).", playlist_name, len(items))

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
            existing.delete()
            log.info("Removed %s's existing copy of %r before re-syncing.", username, playlist_name)
        except NotFound:
            pass
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not remove existing playlist for %s: %s", username, exc)

        try:
            # Re-fetch each item through the target user's own connection so
            # the new playlist is created in that user's context.
            user_items = [user_server.fetchItem(item.ratingKey) for item in items]
            user_server.createPlaylist(playlist_name, items=user_items)
            log.info("Synced %r to %s (%d item(s)).", playlist_name, username, len(user_items))
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to create playlist for %s: %s", username, exc)


def main() -> None:
    url, token, playlist_names, target_users, interval_hours = get_config()
    log.info(
        "Starting playlist sync: %s -> %s, every %s hour(s).",
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
