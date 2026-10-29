# Expansion NTFS rescue handoff — 2026-10-24

## Objective

Preserve the allocated data from the old Expansion NTFS volume before any
formatting. The source UUID is `16AA6BFAAA6BD531`; the verified sg-ultra
destination UUID is `2C30-310F`.

## Evidence and decision

Logical rsync traversal repeatedly blocks in `bread_gfp` on scattered source
branches, although a direct 256 MiB model read completed. Readable analyzer
and collector leaves already metadata-match sg-ultra; blocked leaves remain on
Expansion. This does not authorize deletion or formatting.

`/usr/sbin/ntfsclone` supports `--rescue --save-image`, copies allocated NTFS
data at sector level, and fills unreadable sectors while continuing. It is the
next recovery path, not a claim that every recovered sector is valid.

## Attempted command and result

After stopping only the Expansion-bound paused analyzer containers and
unmounting `/media/seagate-expansion`, one managed job was run:

```text
/usr/sbin/ntfsclone --rescue --save-image \
  --output /media/sg-ultra/recovery/expansion-16AA6BFAAA6BD531.ntfsclone.img \
  /dev/disk/by-uuid/16AA6BFAAA6BD531
```

It blocked in kernel I/O before writing image data. The retained output is a
zero-byte placeholder and is not a recovery image. The source was remounted by
UUID and the analyzer containers restored to their prior paused state.

Do not mount, overwrite, delete, or format the source during a future raw
recovery attempt. A different recovery-capable I/O path and/or additional
imaging storage is required because rescue mode cannot progress while device
reads hang rather than return errors.
