# Operating Kya Pehnu

A short runbook for one person running the stack on one computer. There is no metrics stack: you check a few things by hand.

## Healthy

```
docker compose ps                      # valkey, backend, frontend: all "healthy"; restarts do not climb
curl -fsS http://127.0.0.1:8080/livez  # {"status":"ok"} or 200: the frontend container is up
curl -fsS http://127.0.0.1:8080/readyz # 200: backend can reach SQLite, Valkey and the picture store
```

`/readyz` answering 503 means the backend cannot reach the database, Valkey or the picture store (`event=not_ready failed=files`). `/livez` only says the process runs.

## Signals worth a look (weekly, or when something feels off)

| Signal | Check | Meaning and action |
|---|---|---|
| Backend restarts | `docker compose ps`; `docker inspect -f '{{.RestartCount}}' kyapehnu-backend-1` | More than 0 since last start: read `docker compose logs backend` for the crash. Common causes: out of memory, a refused database (`startup_refused reason=pre_ownership_db`). |
| `/readyz` 503 | `curl -i .../readyz`, then `docker compose ps valkey` | Valkey down or unhealthy: `docker compose up -d --wait`. Database: look for `db_busy` or disk errors in backend logs. |
| Valkey memory | `docker compose exec valkey sh -c 'REDISCLI_AUTH=$(cat /run/secrets/valkey_password) valkey-cli info memory \| grep -E "used_memory_human\|maxmemory_human"'` and `info stats \| grep evicted_keys` | Cap is 128 MB, policy `volatile-lru`. Evictions mean people get signed out early; a personal install should show 0. If it is not 0, something is writing far too much: restart Valkey and report it. |
| SQLite busy | `docker compose logs backend \| grep db_busy` | Occasional 503 `db_busy` (retry after 1 s) while an import runs is normal. Constant ones: a second writer is running (`docker compose ps -a`, stop stray `run` containers). |
| Picture store | `docker compose logs backend \| grep -E "not_ready\|files_store_unavailable"` | `event=not_ready failed=files` with `files_store_unavailable op=... error=...` (S3 only): the bucket, the role policy or the network. Pictures answer 503 `files_unavailable`, nothing is lost; it heals by itself when S3 answers. |
| Disk | `docker system df -v \| grep kyapehnu-data`; `df -h` on the Docker disk | A full disk answers 507 and loses nothing, but stops uploads. Delete old backups in `/data/backups`, free host disk. Keep 20% free. |

## Logs without leaking

- Access logs are off in both uvicorn and nginx, so request URLs (OAuth `code`, `state`, weather place) are not logged.
- The app logs `key=value` events with `request_id` and `user_id`, never email or name: `signin_ok`, `signin_failed reason=...`, `signout`, `rate_limited`, `session_rejected`, `valkey_unavailable`, `admin_denied`, `startup_refused`, `import_done`.
- Read: `make logs`, or `docker compose logs --since 1h backend`.
- nginx may log a rare `crit` line that carries a request line. Before sharing logs with anyone: `grep -v crit`, and look through the rest.
- Do not turn on debug or access logging to diagnose; use `request_id` from the error response instead.

## Backups

- Cadence: before every update and weekly (for example a Sunday `make backup`); also after a big import. Keep the last 4.
- Take and copy out: `make backup`, then `docker compose cp backend:/data/backups/kyapehnu-<timestamp>.zip ~/safe-place/`. A backup left only inside the volume dies with the volume.
- Backups are unencrypted and hold every account. Store them accordingly.
- A backup holds the pictures that are in the store when it runs, from the disk or from the bucket alike. The nightly archives on AWS are not the picture store: they keep deleted pictures for at most 30 days (the backups bucket expires an archive after 15 days and its old version 15 days later), so a deleted picture is gone from the store at once and from the archives within 30 days.
- Restore drill (do it once after first setup, then every few months), on a throwaway project so nothing real is touched:
  1. `COMPOSE_PROJECT_NAME=kyapehnudrill KYAPEHNU_PORT=8099 docker compose up -d --build --wait`
  2. `docker compose -p kyapehnudrill stop backend`
  3. `docker compose -p kyapehnudrill run --rm --no-deps -v "$PWD/kyapehnu-<timestamp>.zip:/restore/backup.zip:ro" backend kyapehnu restore /restore/backup.zip`
  4. `docker compose -p kyapehnudrill up -d --wait`, open http://127.0.0.1:8099, sign in, check the wardrobe and photos are there.
  5. `docker compose -p kyapehnudrill down -v`, and confirm with `docker ps`.
  Restore only works into a stack with no accounts yet; it refuses otherwise and changes nothing.
  A drill stack uses its own bucket (or the local store, with `KYAPEHNU_FILES_BUCKET` empty), never the production bucket: restore writes pictures into whatever store it is set to, and the orphan sweep of a stack deletes pictures that none of its rows names.

## Pictures in S3

`KYAPEHNU_FILES_BUCKET` set means pictures live in that bucket; empty (a laptop) means `/data/images` on the volume. `KYAPEHNU_FILES_REGION` defaults to `ap-south-1`. Everything below is for the AWS stack; the commands run in the backend container (`docker compose run --rm --no-deps backend kyapehnu ...`, with `-f compose.aws.yaml` there).

**Move to the bucket (cut-over)**
2. Downtime: `kyapehnu backup`, stop the backend, `kyapehnu copy-pictures --to s3` with the bucket set (`-e KYAPEHNU_FILES_BUCKET=...`). It prints `pictures_copied copied=N present=N failed=0 deleted=0 direction=s3`; `failed` must be 0. Set the bucket in `.env`, start the backend, open a few pictures.
3. Rollback, any time before step 4: stop the backend, `kyapehnu copy-pictures --to local` (brings back what was created on S3, skips identical files), empty the bucket setting, start. Nothing is lost.
4. After one release: `kyapehnu copy-pictures --to s3 --delete-source` reads each object's SHA-256 in the bucket again and only then deletes the local file. Then `find /data/images -type f | wc -l` prints 0.

A bucket object with other bytes than the disk file is counted `failed` and never overwritten; `--delete-source` with `--to local` is refused; the command never opens the database. Exit code 1 means `failed` above 0, so read the counts before going on.

**The sweep guard.** Housekeeping at start deletes pictures no row names (older than an hour). It deletes nothing when more than `KYAPEHNU_SWEEP_MAX_DELETES` (default 500) would go or when a user has pictures but no rows, and logs `orphan_sweep_skipped reason=over_limit|no_rows_for_user|store_down`. That is a net, not a plan: a drill stack must have its own bucket.

**Release checks (manual).**
- Speed (AC-25): load the wardrobe, then `for i in $(seq 20); do curl -s -o /dev/null -w '%{time_starttransfer}\n' -b "$COOKIE" "$BASE/images/<id>/thumb/<name>.webp"; done | sort -n | tail -n 1` stays under 0.3 s on production data; a repeat with `-H 'If-None-Match: <the ETag of the first answer>'` answers 304 with no body.
- Disk (AC-34): `find /data/images -type f | wc -l` prints 0 once step 4 is done.
- Outage: with S3 unreachable, `docker compose logs backend` shows `not_ready failed=files` and `files_store_unavailable`; it recovers by itself.

## Sign-in fails

1. Browser shows `auth_not_configured` (or `/api/auth/me` answers 401 `auth_not_configured`): `KYAPEHNU_GOOGLE_CLIENT_ID` or `KYAPEHNU_GOOGLE_CLIENT_SECRET` is empty in `.env`. Fill both, `make up`. Backend log has `event=auth_not_configured missing=...`.
2. Google says `redirect_uri_mismatch`: the authorized redirect URI in the Google console must equal `KYAPEHNU_PUBLIC_URL` plus `/api/auth/google/callback` exactly (scheme, host, port).
3. `signin_failed reason=google_unreachable` / 503: the computer is offline or cannot reach accounts.google.com. Sign-in needs the internet; existing sessions keep working.
4. `signin_failed reason=state` or `token`: usually an expired or reused sign-in tab, a blocked cookie, or the system clock being wrong (the check: `date`; enable automatic time, restart Docker Desktop, because its VM clock can drift after sleep). Retry from a fresh tab.
5. `signin_failed reason=unverified_email`: the Google account has no verified email.
6. `rate_limited` (429): 20 attempts a minute are shared by everyone on this computer. Wait a minute.
7. `valkey_unavailable` / 503 `service_unavailable`: see Valkey below.

## Valkey restart

`docker compose restart valkey`. Everyone is signed out and rate limits and chat locks reset. Data is untouched: it lives in SQLite on `kyapehnu-data`, Valkey holds nothing saved. A turn running in chat may need to be sent again. Then `docker compose ps` and `/readyz`.

## Upgrade

1. `make backup` and copy the zip out (the ownership upgrade cannot be undone; databases from before accounts are refused and must come through `import-phase3`, see README "Migrating").
2. `git pull`, `make up`. The database migrates itself on start.
3. Verify: `docker compose ps` all healthy, `/readyz` 200, sign in, open the wardrobe.
4. Watch the backend for 10 minutes: `docker compose logs -f backend` (no restart loop, no `startup_refused`).

## Rollback

Code alone cannot go back after the schema moved. Rollback is restore:

1. `git checkout <previous tag or commit>`.
2. `docker compose down -v` (deletes the current data; make sure the backup zip is copied out).
3. `docker compose up -d --build --wait` for the old version, `docker compose stop backend`, run the `kyapehnu restore` command from Backups, `docker compose up -d --wait`.
4. Enter the AI provider key again in Settings; everyone signs in again.

Roll back when: the backend restarts repeatedly after an upgrade, `/readyz` stays 503 after 5 minutes, or the wardrobe data is missing or wrong. Restore needs an older backup made by the same version as the code you roll back to; a backup from a newer version is refused.

## Targets (informal, personal use)

- `/readyz` 200 whenever the stack is up; backend recovers by itself within about a minute after a restart.
- No backup older than 7 days.
- Free disk above 20%.
No uptime SLO or paging: it is a laptop.

### AWS pilot: picture store and chat history (SLOs)

No CloudWatch, no paging (delivery D8). Measure by hand with `/readyz`, `docker compose logs` and the curl loop in "Speed".

| SLI | SLO | How to check | Source |
|---|---|---|---|
| Thumb time to first byte | p95 at most 0.3 s; repeat view answers 304 | the "Speed" curl loop, at cut-over and after each release | NFR-1, AC-25 |
| History list (1,000 chats) | p95 at most 0.3 s | same loop on the chat list endpoint | NFR-2 |
| Picture store reachable | `/readyz` 200 whenever the stack is up; an S3 blip heals by itself without a restart | `/readyz`; no `not_ready failed=files` older than 5 minutes | NFR (availability) |
| Cost | pictures bucket at most US$2 per month | Cost Explorer filtered to the bucket, 30 days after cut-over, then monthly | NFR-10, AC-33 |

Error budget: a few minutes of `files_unavailable` a month is fine (nothing is lost). Any `orphans_swept` above 0 that you did not expect is not.

### Events to watch (picture store)

`docker compose logs --since 24h backend | grep -E 'files_unavailable|files_store_unavailable|files_delete_failed|orphans_swept|orphan_sweep_skipped|mannequin_cleanup_skipped|collage_failed'`

| Event | Meaning | Action |
|---|---|---|
| `files_unavailable` (503 code) / `files_store_unavailable op=..` | S3 call failed (credentials, policy, network, outage). Nothing lost. | Incident "S3 down" or "readyz files FAIL" below. |
| `files_delete_failed error=.. left=N` | a delete after a row removal did not reach S3; the object stays until the next sweep | One-off: ignore. Repeating: same as S3 down. Check the sweep ran after. |
| `orphans_swept count=N` | start-up housekeeping deleted N unreferenced pictures | N is usually 0 to a handful. Large N (hundreds): incident "sweep deleted too much". |
| `orphan_sweep_skipped reason=over_limit\|no_rows_for_user\|store_down` | the guard refused to delete | `over_limit` or `no_rows_for_user`: find out why before anything else (wrong database or wrong bucket for this stack). `store_down`: S3 down. |
| `mannequin_cleanup_skipped`, `collage_failed` | an outfit side step failed (old mannequin file kept, collage not made); the user action still worked | Occasional: ignore. Together with `files_unavailable`: S3 problem. |


| Step | Check (read-only) | Pass | Roll back when |
|---|---|---|---|
| 1 upgrade, disk store | `/readyz`; `grep -c files_store_unavailable`; `docker inspect -f '{{.RestartCount}}' kyapehnu-backend-1`; watch `logs -f backend` 10 min | 200, 0, 0, no `startup_refused` | restarts climb, `/readyz` 503 after 5 min: step 1 rollback |
| 2 provision | `check-pictures-bucket.sh` before detach, after detach, `IMDS=1` on the host | no FAIL (SKIP only as the runbook allows) | nothing to roll back (additive); any FAIL blocks step 3 |
| 3 cut-over | `pictures_copied ... failed=0`, `copied+present=N`; then `/readyz`, S3 listing, 304 and 206 checks, Speed loop; logs 30 min | all hold; p95 under 0.3 s; no `files_unavailable` | `failed>0` before the bucket is set: abort. After: `/readyz` 503 over 5 min, `files_unavailable` repeating, or p95 over 0.3 s after one retry: `copy-pictures --to local` |
| 4 soak (1 to 2 weeks) | daily for the first 3 days, then weekly: `/readyz`, the events grep above, `orphans_swept` values, bucket object count vs. the wardrobe | no new event kinds, sweeps near 0 | as step 3 (still possible) |
| 5 delete disk copies | `failed=0`, `find /data/images -type f \| wc -l` is 0, `/readyz`, open 3 pictures | all hold | none: restore from backup (30 days) |
| +30 days | Cost Explorer for the bucket | at most US$2 | review lifecycle and request volume |

### Incidents (picture store)

Always first: `curl -i http://127.0.0.1:8080/readyz`, then `docker compose logs --since 30m backend | grep -E 'not_ready|files_'`. On AWS, `C='docker compose -f compose.yaml -f compose.aws.yaml'`, bucket `<your-bucket>`.

**S3 down (pictures answer 503 `files_unavailable`, `/readyz` 503 `failed=files`).**
1. Rule out the cause on our side: is the error `AccessDenied` (see "credentials" below) or a timeout/5xx (S3 or network)?
2. Check the AWS Health dashboard for S3 in ap-south-1 and `curl -sI https://s3.ap-south-1.amazonaws.com`.
3. S3 outage: wait. Nothing is lost, uploads fail with 503, the backend recovers by itself. Do not restart the stack and do not run the sweep by hand. The start-up sweep refuses to delete while the store is down (`orphan_sweep_skipped reason=store_down`).
4. Over 1 hour or the owner needs pictures now: no fallback mirror exists after step 5 (disk copies deleted); before step 5 you may use the step 3 rollback only when S3 is reachable (it copies back from S3). Otherwise wait.
5. Escalation: AWS Support case if the Health dashboard shows nothing and the role policy is unchanged.

**Credentials or hop limit broken (`/readyz` shows files FAIL, error `AccessDenied`, `NoCredentialsError` or IMDS timeout).**
2. Hop limit: `aws ec2 modify-instance-metadata-options --instance-id <ID> --http-tokens required --http-put-response-hop-limit 2` (laptop, no restart), then re-run the check.
4. Verify `/readyz` 200 and open a picture. The store heals without a restart.

**Sweep deleted too much (pictures missing, `orphans_swept count=` large, or a stack pointed at the wrong bucket).**
1. Stop the cause first: `$C stop backend` (and stop any laptop or drill stack that has the production bucket in its settings). The guard caps one start at 500 deletes; every restart can delete 500 more.
2. Pictures versions are kept 7 days. List what was deleted: `aws s3api list-object-versions --bucket <your-bucket> --prefix images/ --query 'DeleteMarkers[?IsLatest].[Key,VersionId,LastModified]' --output text`.
3. Restore by removing the delete marker of each key: `aws s3api delete-object --bucket <your-bucket> --key <key> --version-id <marker VersionId>`. Loop over the list from step 2, limited to markers from the incident time. The instance role cannot delete versions, so run this from the laptop as `kyapehnu`.
4. Fix the cause (wrong `KYAPEHNU_FILES_BUCKET`, wrong database volume, `no_rows_for_user`) before `$C up -d --wait`. Check pictures open; compare the object count to the wardrobe.
5. After 7 days the versions expire: a late find means restore from a backup archive (30 days, `kyapehnu restore` into a drill stack, then copy the pictures).

**`copy-pictures` failed mid-way (exit 1, `failed>0`, or the command was cut off).**
1. It is idempotent: it skips identical files and never overwrites a differing object. Just read the last line `pictures_copied copied=X present=Y failed=Z`.
2. Cut-over (step 3): do not set the bucket. Find the cause (policy, network, a bucket object with other bytes: it is named in the output), fix, rerun the same command until `failed=0` and `X+Y = N`. To abort: `$C up -d --wait`; pictures are still on disk.
3. Step 5 (`--delete-source`): a file stays on disk when its bucket check fails, so a partial run loses nothing. Rerun, or `$C up -d --wait` and retry another day. Take `backup.sh` before every attempt.
4. If the backend was stopped and the command died: `$C up -d --wait` is always safe at steps 3 and 5 only when the bucket setting in `.env` matches where the pictures really are (before the bucket is set: disk, after: S3).
