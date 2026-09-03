# Working in this repo

House conventions that are easy to violate because they are not visible from any single file.
They are written down because each one has already cost something.

## Before pushing

The suite must be green. `./test-all.sh` runs every `test_*pub*.py` on the host in seconds, and
the `pre-push` hook enforces it (activate once: `git config core.hooksPath githooks`).

Update the tests **in the same change** as the behaviour. On 2026-07-16 the `read_growatt` and
`read_p1` suites had been red for six weeks against code that was correct and running — the tests
had drifted while the code moved on. Nobody noticed, because nothing ran them.

When a test fails, first work out which side is wrong. If the code is right and the test drifted,
fix the test to the code — and say so, rather than quietly adjusting the expectation.

## Do not leave `.bak` copies of versioned files

The old habit was a timestamped `.bak` before every edit. That made sense while the files were
unversioned; it does not any more, and `*.bak*` is now git-ignored so a stray one cannot be
committed by accident.

**If the file is in a git repo, git is the backup.** Commit before the risky edit, then use
`git diff`, `git revert` or `git checkout --` to get back. A `.bak` next to a tracked file is
strictly worse: it duplicates what git already holds, and when there are twelve of them nobody
can tell which one was the good version. The kadence-child theme had 75 of them (3.0 MB) by
2026-08-13, and `battery-dashboard-GC.php.bak-hourspread-…` turned out to be byte-identical to a
commit made an hour earlier.

Two repos cover the code:

| Repo | Covers |
|---|---|
| `home-energy-system` (this one) | every service on pi5new |
| `erix-wordpress` (private) | the `kadence-child` theme on pi5 — see the wordpress memory |

**Outside those, take care but do not reach for `.bak` either.** For anything gitignored the
answer is usually restic (the nightly backup covers the whole docker root on pi5), not a copy
beside the original. Never copy `.env` at all — a `.env.bak` is a second unprotected file full of
secrets, and one already existed on pi5 for months.

## Tests

- One `test_*pub*.py` per service; `test-all.sh` auto-discovers any directory containing one.
- **They run on the host, not in the container.** No hardware, no database, no network. Stub the
  heavy or device-specific imports with `MagicMock` *before* importing the module (`pymodbus`,
  `serial`, `eccodes`, `mysql.connector`, `paho`, `dotenv`, `requests`). Stubbing `dotenv` also
  keeps the real `.env` out of the test.
- Set the env vars the module reads at import time before importing it, with neutral values.
  Never real coordinates, hosts or keys — this repo is public.
- **Never write to the database.** Mock the connection and assert against the rows that would
  have been written.
- Assert against the real function. A test that reimplements production logic in a mock and then
  checks the mock passes forever and proves nothing — `TestStandbyRouting` did exactly that and
  survived the deletion of the behaviour it claimed to cover. If logic can only be tested by
  rebuilding it in the test, that is the signal to extract it from `main()`, not to rebuild it.

## Deploying a change

Which command applies your edit depends on how the service gets its code. Getting this wrong looks
like the change silently not working:

| Service | Code arrives via | Applies with |
|---|---|---|
| **all** pi5new services + surebet on pi5 | bind-mount `.:/app` | `docker compose restart` |

Since 2026-09-03 **every** service is bind-mount: the compose mounts `.:/app` (host code shadows the
image), and the Dockerfile copies only `requirements.txt` + `entrypoint.sh` — no app `.py`. So a code
or entrypoint edit applies on plain `docker compose restart`; no build, ever. Verified live (container
`/app` md5 == host). `common/` is bind-mounted (`../common:/app/common:ro`) into the services that use
it, so a change to a shared constant applies on `restart` too. Adding or changing a **volume/mount** in
the compose still needs `up -d` (not just restart) to recreate the container; a Dockerfile/base-image or
`requirements.txt` change still needs `build && up -d`.

Before 2026-09-03 four services (battery_optimizer, read_bmw, read_otthing, transfer_p60) were `COPY`-
based and needed a build per edit; they were converted to bind-mount for consistency (one host, source
always present, least friction). `./rebuild-all.sh` still does `down` + `up -d --build` for every service
with a compose file when you do want a full rebuild.

## Logging

Python logs to **stdout only**. `entrypoint.sh` tees it into `/logs/debug_$(date +%F).log`:

```sh
exec python3 -u <service>.py 2>&1 | while IFS= read -r line; do
    printf '%s\n' "$line"                                         # keep stdout (docker logs)
    printf '%s\n' "$line" >> "${LOG_DIR}/debug_$(date +%Y-%m-%d).log"
done
```

Do not add a `FileHandler` — it breaks the convention and makes the module unimportable on the
host, which breaks the test suite.

To read logs, grep `<service>/logs/debug_YYYY-MM-DD.log`, not `docker logs` — container output is
lost on rebuild. The date is re-evaluated **per line**, so the file rolls at midnight to the new
day on its own — one file per calendar day, regardless of how long the container has been up.
(Until 2026-09-03 the name was fixed at container-start date, so a long-running container grew one
giant file — read_seplos hit 1.35 GB; that is fixed.) `cleanup_logs.sh` (root cron, daily) then
deletes files whose **filename date** is older than 5 days — by name, not mtime, since the active
file's mtime never ages.

Log in local time. The database stores local time; a log in UTC sits two hours off the rows it
just wrote.

## The database

`mariadb/schema.sql` is the record of what `erix_db` looks like. Services create their own tables
with `CREATE TABLE IF NOT EXISTS`, so **schema.sql does not create anything** — it documents, and
therefore drifts unless you update it in the same change. Five tables had gone undocumented by
2026-07-16.

`python3 tools/check_schema.py` compares the live database against schema.sql. The pre-push hook
runs it as advisory (it cannot block, since a push may come from a machine without the DB).

Take definitions from `SHOW CREATE TABLE`, and write down *why* the table is shaped that way —
the column list is already in the database; the reasoning is not.

### Writing to `energy`

One row per 5-minute interval, six services, and **nobody owns the row**. Address it by its
timestamp — never by "the newest row":

```python
from common import energy_row as er
cur.execute(er.upsert_sql(["my_col", ...]), (er.bucket(), value, ...))
```

`er.bucket()` is the interval, `er.upsert_sql()` names only your own columns, so whoever runs
first creates the row and nobody can blank out another service's data. A service that misses a
cycle leaves its own columns NULL, which is honest; `UPDATE ... ORDER BY id DESC LIMIT 1` instead
wrote to whatever row was newest and silently landed on the previous interval whenever this one
did not exist yet. That cost ~1.4% of realised cost every day until 2026-07-16.

Reading the newest row is fine — it is writing to it that is the bug.

## Where things live

- Compute in the software, before the DB write. The dashboard reads the resolved answer out of
  the database; it does not re-derive control logic (e.g. `energy.control_action`, not a deadband
  reimplemented in PHP).
- `common/` holds what more than one service must agree on — `energy_cost.py` is the single source
  for the all-in/saldering cost formula.
- `read_seplos` is the sole writer of the Seplos BMS PCS registers and the sole user of the RS485
  bus to the battery. Nothing else opens `/dev/tty_seplos` — not even to hold it unused.

## This repo is public

No coordinates, no hostnames, no keys, no personal data — in code, tests, docs or screenshots.
Real values come from `.env` (git-ignored); defaults in code are generic. Check before pushing.
