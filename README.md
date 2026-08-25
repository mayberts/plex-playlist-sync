# plex-playlist-sync

Keeps a copy of one or more playlists (e.g. **"Kate's favourites"**) in sync
for one or more Plex Home users, re-syncing once a day. Runs as a small
Docker sidecar next to your Plex server — it does not modify Plex itself.

## Why this exists

Plex's built-in "Share" on a playlist only hands out a one-time, read-only
snapshot — if you add or remove items later, people you shared with never
see the update, and it doesn't reliably show up in Plexamp. There's no
native toggle to keep a playlist "live" across Home users. This script
closes that gap by periodically diffing each target user's copy against
your current playlist and adding/removing only what changed — the playlist
itself is never deleted and recreated, so it keeps the same identity across
syncs (the first sync for a new user does create it from scratch, since
there's nothing yet to diff against).

**This is one-directional**: your edits flow to Kate. Items you remove from
the source are removed from Kate's copy too. Items *she* removes come back
on the next sync if they're still in the source; anything still present in
both copies is left untouched, including her reordering. New items are
appended to the end of her copy rather than inserted to match your order.

## 1. Get your Plex admin token (X-Plex-Token)

You need the **server owner's** token — switching into another user's
account is an admin-only operation.

1. Sign into the Plex web app (`app.plex.tv`) as the server owner.
2. Open any item in your library, click the **⋮** (more) menu → **Get Info**,
   then click **View XML** (bottom right of the dialog).
3. A new tab opens with a URL containing `X-Plex-Token=xxxxxxxxxxxx` —
   copy that token value.

(Full official steps, if the above ever changes in the Plex UI: search
Plex's support site for "Finding an authentication token / X-Plex-Token".)

Treat this token like a password — anyone with it has full access to your
server.

## 2. Find your Plex server's local address

Usually `http://<the-machine's-LAN-IP>:32400`, e.g. `http://192.168.1.50:32400`.
You can confirm it in Plex under **Settings → Network**.

If this sync container will run on the **same Docker host** as Plex itself,
you can instead use `http://host.docker.internal:32400` (Docker Desktop) or
the Plex container's name on a shared Docker network — see the commented-out
`networks:` section in `docker-compose.yml`.

## 3. Configure

```bash
cp .env.example .env
```

Edit `.env`:

```
PLEX_URL=http://192.168.1.50:32400
PLEX_TOKEN=<paste your token here>
PLAYLIST_NAMES=Kate's favourites
TARGET_USERS=Kate
SYNC_INTERVAL_HOURS=24
```

- `PLAYLIST_NAMES` accepts a comma-separated list, e.g.
  `Kate's favourites,Road trip mix`. Each name must match its playlist title
  exactly (case-sensitive).
- `TARGET_USERS` accepts a comma-separated list if you want to sync to more
  than one Home user, e.g. `Kate,Alex`.
- Every playlist in `PLAYLIST_NAMES` is synced to every user in
  `TARGET_USERS` — there's no way to send different playlists to different
  users from a single container (see "Adjusting later" below for that case).

## 4. Run it

The image is published to GitHub Container Registry on every push to `main`,
so you can just pull and run it — no build step needed on the target host:

```bash
docker compose up -d
```

If you'd rather build locally (e.g. after editing `sync_playlist.py`),
comment out the `image:` line in `docker-compose.yml`, uncomment `build: .`,
and run `docker compose up -d --build` instead.

> **Note:** if the GHCR package is private, `docker compose pull` will fail
> with an authentication error. Either make the package public (Package
> settings → Change visibility, on the package page under your GitHub
> profile), or run `docker login ghcr.io -u <your-username>` on the Unraid
> host first, using a [personal access token](https://github.com/settings/tokens)
> with `read:packages` scope as the password.

Check it's working:

```bash
docker compose logs -f
```

You should see a line like:

```
Starting playlist sync: "Kate's favourites" -> Kate, every 24.0 hour(s).
...
Synced "Kate's favourites" for Kate: removed 0, added 2 (14 item(s) total).
```

(The first sync for a user instead logs `Created "Kate's favourites" for Kate
(12 item(s)).` since there's no existing copy to diff against yet.)

With multiple playlists and/or users configured, each playlist is synced to
each user in turn within the same run.

The container then sleeps and re-runs the sync every `SYNC_INTERVAL_HOURS`
(daily, by default), for as long as it's running — `restart: unless-stopped`
means it comes back automatically after a host reboot too.

## Limitations to know about

- **Smart playlists** (the rule-based, auto-updating kind) are skipped —
  there's nothing fixed to copy. Convert it to a regular playlist first if
  you want it synced this way.
- Kate's copy is a real, separate playlist under her account. Her reordering
  and any items she keeps that are still in the source are left alone; only
  the add/remove diff against the source is applied each sync.
- New items land at the **end** of Kate's playlist, not necessarily in the
  same position as in the source — reordering existing items is never done.
- If a target username is mistyped, or the playlist name doesn't match
  exactly, the log will say so clearly — check `docker compose logs`.

## Adjusting later

- Change `SYNC_INTERVAL_HOURS`, `PLAYLIST_NAMES`, or `TARGET_USERS` in
  `.env`, then:
  ```bash
  docker compose up -d
  ```
  (no rebuild needed — it's just an environment variable change, Compose
  will recreate the container.)
- To add a playlist, append it to the comma-separated `PLAYLIST_NAMES` list.
- If you need different playlists going to different sets of users, run a
  second copy of this container: duplicate the project (or just add a
  second `service:` block in `docker-compose.yml`, pointing to a second
  `.env` file) with its own `PLAYLIST_NAMES`/`TARGET_USERS` and a different
  `container_name`.
