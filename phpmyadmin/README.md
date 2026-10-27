# phpmyadmin

phpMyAdmin 5.2 (official multi-arch image) — web interface for the MariaDB database.

## Access

```
http://<pi-ip>/
```

Apache listens on port 80 directly (LAN only, no reverse proxy). The container uses `network_mode: host` and connects to MariaDB on `127.0.0.1:3306`.

## Setup

```bash
cd ~/docker/phpmyadmin
docker compose --env-file "../.env" up -d
```

Credentials are taken from `PMA_USER` / `PMA_PASSWORD` in the docker-compose, which use `DB_RESOL_USER` and `DB_RESOL_PASSWORD` from `.env` (the MariaDB root account).

## Notes

- `phpmyadmin:5.2` follows 5.2.x patch releases and PHP base-image rebuilds; update with
  `docker compose pull && docker compose --env-file ../.env up -d`. Moved off `arm64v8/phpmyadmin:5.1`
  (PHP 8.0, EOL) on 2026-10-27 for CVE-2026-109618/109619.
- Smoke-test an update like a browser does: open a table (`index.php?route=/sql&db=<db>&table=<t>&pos=0`)
  or POST a query with the session token. Do **not** pass `sql_query=` in a GET URL — 5.2 ignores an
  unsigned query there, runs an empty one and shows "Internal error: ValueError", which looks like a
  broken release but is not.
- Without `--env-file ../.env` the credentials are empty ("Access denied for user ''").
- No data is stored in this container — it is stateless.
