# phpmyadmin

phpMyAdmin 5.1 (ARM64) — web interface for the MariaDB database.

## Access

```
http://<pi-ip>:8082
```

Port is configured in the phpMyAdmin reverse proxy / nginx config. The container uses `network_mode: host` and connects to MariaDB on `127.0.0.1:3306`.

## Setup

```bash
cd ~/docker/phpmyadmin
docker compose --env-file "../.env" up -d
```

Credentials are taken from `PMA_USER` / `PMA_PASSWORD` in the docker-compose, which use `DB_RESOL_USER` and `DB_RESOL_PASSWORD` from `.env` (the MariaDB root account).

## Notes

- Uses the `arm64v8/phpmyadmin:5.1` image for Raspberry Pi 5 (ARM64).
- Custom phpMyAdmin configuration is in `config.user.inc.php` (volume-mounted read-only).
- No data is stored in this container — it is stateless.
