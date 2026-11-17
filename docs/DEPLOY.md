# Deployment runbook

Operational procedure for taking a phase from this checkout to Ares and back
again. Nothing here changes agent behaviour; it exists so that schema
migrations and phase upgrades are rehearsed on staging and reversible.

## Prerequisites and port note

- Docker 24+ with Compose v2; the model endpoint reachable from the
  container (`HARNESS_MODEL_BASE_URL`).
- The harness **and SearXNG both default to host port 8080**. Staging must
  therefore use a different host port (`HARNESS_STAGE_PORT`, default 8081);
  the container itself always listens on 8080.

## 0. Install from zero, doctor, and reset

A fresh install needs no hand-edited file. Either let the image entrypoint
bootstrap from the environment (`HARNESS_TOKEN`, `HARNESS_MODEL_BASE_URL`,
`HARNESS_MODEL_NAME`; see the compose-free `docker run` example in the
README), or start the container unconfigured and use the first-run wizard at
`/`.

Run the preflight checks inside the container before mounting real data:

```bash
docker compose exec harness python -m harness doctor
# or: docker compose exec harness python -m harness doctor --json
```

`harness reset --yes` takes a backup, then wipes the database, artifacts,
and module runtime state and (without `--keep-config`) the config, so the
next start is a fresh install:

```bash
docker compose exec harness python -m harness reset --yes
```

The service exits and `restart: unless-stopped` brings it back. On staging,
deleting the `harness-stage-data` volume is the equivalent reset.

## 1. Build and tag an image per phase

Build from the phase's checkout and tag the image per phase so every
promoted phase has a distinct, rerunnable tag:

```bash
docker build --build-arg GIT_SHA=$(git rev-parse --short HEAD) \
  -t personal-harness:<tag> .
```

Example: `personal-harness:8.0`. The sha is baked into the image
(`HARNESS_GIT_SHA`) and reported by `GET /api/system`. Keep the previous
tag in the local image store; it is the rollback target.

## 2. Back up production before upgrading

```bash
python -m harness backup
```

The archive lands in `<data dir>/backups/harness-<timestamp>.zip` (in the
container: the `harness-data` volume, `/data/backups/`). Copy it off the
volume/host before deploying.

On top of that, startup now takes its own safety net: when the server opens
a database whose `schema_meta.schema_version` is below the code's
`SCHEMA_VERSION`, it first writes
`backups/pre-migrate-v<old>-to-v<new>-<timestamp>.zip` into the data
directory (database snapshot plus `config.yaml`) and only then migrates. If
that backup cannot be written, startup aborts with
`pre-migration backup failed at <path>: <reason>` and the schema version is
**not** bumped. These files land in `backups/` next to the database
(`harness-data:/data/backups` in the container).

## 3. Deploy to staging

Staging is a separate compose project (`docker-compose.stage.yml`):
separate service (`harness-stage`), image (`personal-harness:stage`),
token (`HARNESS_STAGE_TOKEN`), and its own volume
(`harness-stage-data` — never `harness-data`; one writer per data
directory). It has **no MCP bind mounts** by design: staging starts from a
restored backup or from zero.

```bash
export HARNESS_STAGE_TOKEN=<stage-token>
export HARNESS_STAGE_PORT=8081   # not 8080 (SearXNG)
docker compose -f docker-compose.stage.yml up -d --build
```

To rehearse a migration against real data, first restore a production
backup into staging (next section) before the first start.

### Restore a production backup into staging

```bash
# 1. Grab the backup from production (service "harness", /data/backups).
docker compose cp harness:/data/backups/harness-<timestamp>.zip ./

# 2. Start staging empty.
docker compose -f docker-compose.stage.yml up -d --build

# 3. Copy the archive in and restore it; the service restarts itself
#    afterwards (restart: unless-stopped brings it back).
docker compose -f docker-compose.stage.yml cp \
  ./harness-<timestamp>.zip harness-stage:/tmp/restore.zip
curl -sS -X POST http://127.0.0.1:${HARNESS_STAGE_PORT}/api/restore \
  -H "Authorization: Bearer ${HARNESS_STAGE_TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"path": "/tmp/restore.zip", "confirm": true}'
```

Note: the restore also replaces `config.yaml`, so after restoring
production state the staging token is the **production** token; update
`HARNESS_STAGE_TOKEN` (or the file) to match.

## 4. Verify

```bash
curl -sS http://127.0.0.1:8081/health
# -> {"status":"ok"}
curl -sS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8081/api/system
# -> JSON including version, git_sha, schema_version, data_dir
```

Then:

- Smoke check the main stack (build, start, health, auth, API shape,
  endpoint probe):
  `HARNESS_TOKEN=check-token HARNESS_PORT=18080 python scripts/container_check.py`
- Run the deterministic suite **inside the image** so green is confirmed on
  Linux, not just on the dev machine:
  ```bash
  docker build --target test -t personal-harness:<tag>-tests .
  docker run --rm personal-harness:<tag>-tests
  ```
- Run the live evaluations against the model:
  `python -m harness eval --mode live`
  (in-process; never touches the deployed data directory).

## 5. Promote to production

When staging has been followed end to end:

1. Back up production (`python -m harness backup`) and keep the archive off
   the host.
2. Build the new tag (step 1) and point production at it: set the
   `image:` value in `docker-compose.yml` to the new tag (or rebuild with
   `docker compose up -d --build` from the phase checkout).
3. `docker compose up -d`; wait for the healthcheck; check
   `/api/system` (`version`, `git_sha`, `schema_version`) to confirm the
   right build is running.

## 6. Roll back

1. Put the previous image tag back in `docker-compose.yml` (the old tag is
   still in the local image store) and `docker compose up -d`.
2. Restore the pre-upgrade backup, same mechanics as the staging restore in
   step 3: copy the archive into the production container
   (`docker compose cp ./harness-<timestamp>.zip harness:/tmp/restore.zip`)
   and call `POST /api/restore` with
   `{"path": "/tmp/restore.zip", "confirm": true}`; the service restarts
   itself.
3. Verify `/health` and `/api/system` again. The automatic pre-migration
   backup (`backups/pre-migrate-v*-to-v*.zip`) is an additional fallback if
   the upgrade failed while migrating.
