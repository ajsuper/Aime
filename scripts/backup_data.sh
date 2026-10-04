#!/usr/bin/env bash
# Snapshot a running Aime container's /data volume to a dated .tgz on the host.
#
#   ./scripts/backup_data.sh [container] [dest_dir] [keep]
#   defaults:                aime-aime-1  /mnt/docker/aime-backups  14
#
# The databases run in WAL mode, so tarring the live files can capture a torn
# copy. Each *.sql is instead copied with SQLite's online backup API (inside the
# container, which has python3) and the snapshot goes into the archive under its
# original path; -wal/-shm files are left out. Everything else is archived as-is.
#
# The archive holds every account, the encryption keys, and the session secret:
# it is written 0600 and should only ever leave the box encrypted.
# Meant for cron, e.g.:  17 4 * * *  /path/to/backup_data.sh >> /var/log/aime-backup.log 2>&1
set -euo pipefail

CONTAINER="${1:-aime-aime-1}"
DEST="${2:-/mnt/docker/aime-backups}"
KEEP="${3:-14}"

STAMP="$(date -u +%Y%m%d-%H%M%SZ)"
OUT="$DEST/$CONTAINER-$STAMP.tgz"
umask 077
mkdir -p "$DEST"

docker exec -i "$CONTAINER" python3 - > "$OUT.partial" <<'PY'
import os, sqlite3, sys, tarfile, tempfile

ROOT = "/data"
with tempfile.TemporaryDirectory() as tmp, \
        tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as tar:
    for dirpath, _, files in os.walk(ROOT):
        for name in sorted(files):
            path = os.path.join(dirpath, name)
            arcname = os.path.relpath(path, ROOT)
            if name.endswith((".sql-wal", ".sql-shm")):
                continue
            if name.endswith(".sql"):
                snap = os.path.join(tmp, "snap.sql")
                src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                dst = sqlite3.connect(snap)
                src.backup(dst)
                dst.close()
                src.close()
                tar.add(snap, arcname=arcname)
                os.remove(snap)
            else:
                tar.add(path, arcname=arcname, recursive=False)
PY

# A truncated stream still leaves a file behind; only keep archives that read back.
tar -tzf "$OUT.partial" > /dev/null
mv "$OUT.partial" "$OUT"
sha256sum "$OUT" > "$OUT.sha256"
echo "$(date -u +%FT%TZ) wrote $OUT ($(du -h "$OUT" | cut -f1))"

# Retention: newest $KEEP archives for this container.
ls -1t "$DEST/$CONTAINER"-*.tgz | tail -n +"$((KEEP + 1))" | while read -r old; do
    rm -f "$old" "$old.sha256"
done
