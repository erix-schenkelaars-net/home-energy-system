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
| read_seplos, read_p1, read_knmi | bind-mount `.:/app` | `docker compose restart` |
| battery_optimizer, read_bmw, read_growatt, dashboards | `COPY` into the image | `docker compose build --no-cache && up -d` |

For image-based services, always `--no-cache`: a cached `COPY` layer will happily ship the old
code. And always pair `build` with `up -d` — building alone leaves the old container running.
`./rebuild-all.sh` does `down` + `up -d --build` for every service that has a compose file.

## Logging

Python logs to **stdout only**. `entrypoint.sh` tees it into `/logs/debug_$(date +%F).log`:

```sh
exec python3 -u <service>.py 2>&1 | tee -a "${LOG_FILE}"
```

Do not add a `FileHandler` — it breaks the convention and makes the module unimportable on the
host, which breaks the test suite.

To read logs, grep `<service>/logs/debug_YYYY-MM-DD.log`, not `docker logs` — container output is
lost on rebuild. The file is named after the date the **container started**, so a container that
has been up for days is still writing to an older file.

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
