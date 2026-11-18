# Storage vault switch handoff

The live UC Compose stack currently bind-mounts `/media/sg-ultra/unifiedcollector`, which is a directory on the 444 GiB root filesystem, not a mount. It has about 79 GiB free. Historical media has a 750 GiB reserve and is therefore permanently paused.

The intended external archive is `/media/seagatefatgreenNADD046Gunified/unifiedcollector` on `/dev/sdf2` (3.6 TiB, about 1.4 TiB free). It is the same directory exposed at `/media/seagate-expansion/unifiedcollector`; it holds about 2.2 TiB of existing UC archive data. The root fallback contains about 3 GiB of newer active artifacts.

Completed safe configuration work:

- Added `UNIFIEDCOLLECTOR_HOST_ROOT=/media/seagatefatgreenNADD046Gunified/unifiedcollector` to ignored `docker/.env`.
- Repaired `/home/bryan/.config/systemd/user/unifiedcollector-compose.service` to use the active checkout, require the external mount, load the project `.env`, and use `docker compose stop` rather than `down`.
- Verified a Compose render with both root `.env` and `docker/.env`: Telegram, WhatsApp, browser-ingest, dashboard, and bridge mounts resolve to the external path.

Completed live sequence: paused only the 21 artifact-mounting containers, recreated them with the corrected Compose environment (no Compose down), then started the repaired user unit. Docker inspection confirms 24 UC services now bind the external vault and none bind `/media/sg-ultra/unifiedcollector`. All UC containers are healthy; dashboard and browser-ingest pass; Telegram's historical-media gate permits work with `1,572,037,722,112` free bytes against the `805,306,368,000` reserve.

In progress: an additive-only exact-path root-to-external reconciliation is still running. The broad rsync copied 3.45 GB before exFAT directory enumeration became pathologically slow; the replacement manifest pass avoids recursive target scans, does not overwrite or delete, and is safe to let finish while the source fallback is static. Keep the old fallback intact until its clean exit. Preserve user-owned YouTube sidecar edits.
