#!/usr/bin/env python3
"""
Pull photo thumbnails and Photos.sqlite from a connected iPhone over AFC,
then classify thumbnails as active, trashed (Recently Deleted), or orphaned
(no matching asset row = likely permanently deleted).

Requires: pip install pymobiledevice3
iPhone must be unlocked and trusted.

Usage: python3 iphone_deleted_thumbs.py [output_dir]
"""
import asyncio
import csv
import os
import shutil
import sqlite3
import sys

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService

OUT = sys.argv[1] if len(sys.argv) > 1 else "iphone_thumbs"
RAW = os.path.join(OUT, "raw")
THUMB_ROOT = "/PhotoData/Thumbnails"
DB_PATH = "/PhotoData/Photos.sqlite"


async def pull(afc, remote, local):
    data = await afc.get_file_contents(remote)
    os.makedirs(os.path.dirname(local), exist_ok=True)
    with open(local, "wb") as f:
        f.write(data)


async def walk(afc, path):
    for name in await afc.listdir(path):
        if name in (".", ".."):
            continue
        full = f"{path}/{name}"
        if await afc.isdir(full):
            async for sub in walk(afc, full):
                yield sub
        else:
            yield full


def load_assets(db_file):
    con = sqlite3.connect(db_file)  # read-write so a pulled WAL is applied to the local copy
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    table = "ZASSET" if "ZASSET" in tables else "ZGENERICASSET"  # ZGENERICASSET on iOS 13 and earlier
    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
    trash_col = "ZTRASHEDSTATE" if "ZTRASHEDSTATE" in cols else "0"
    assets = {}
    for d, fn, trashed in con.execute(f"SELECT ZDIRECTORY, ZFILENAME, {trash_col} FROM {table}"):
        if d and fn:
            assets[(d, fn)] = bool(trashed)
    con.close()
    return assets


def classify(rel, assets):
    # Expected layout: V2/<ZDIRECTORY...>/<ZFILENAME>/<size>.JPG
    parts = rel.split("/")
    if len(parts) < 4 or parts[0] != "V2":
        return "unparsed", "", ""
    directory, filename = "/".join(parts[1:-2]), parts[-2]
    if (directory, filename) not in assets:
        return "orphaned", directory, filename
    return ("trashed" if assets[(directory, filename)] else "active"), directory, filename


async def main():
    lockdown = await create_using_usbmux()
    afc = AfcService(lockdown)

    # Photos database plus WAL/SHM if present
    db_local = os.path.join(RAW, "Photos.sqlite")
    try:
        await pull(afc, DB_PATH, db_local)
    except Exception as e:
        sys.exit(f"Cannot read {DB_PATH} over AFC ({e}). Use the backup method instead.")
    for ext in ("-wal", "-shm"):
        try:
            await pull(afc, DB_PATH + ext, db_local + ext)
        except Exception:
            pass

    # Thumbnails
    try:
        remote_files = [f async for f in walk(afc, THUMB_ROOT)]
    except Exception as e:
        sys.exit(f"Cannot read {THUMB_ROOT} over AFC ({e}). Use the backup method instead.")

    local_files = []
    for remote in remote_files:
        rel = remote[len(THUMB_ROOT) + 1:]
        local = os.path.join(RAW, "Thumbnails", rel)
        try:
            await pull(afc, remote, local)
            local_files.append((rel, local))
        except Exception as e:
            print(f"skip {remote}: {e}")

    assets = load_assets(db_local)

    report = os.path.join(OUT, "report.csv")
    counts = {}
    with open(report, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["status", "directory", "filename", "thumbnail"])
        for rel, local in local_files:
            status, d, fn = classify(rel, assets)
            counts[status] = counts.get(status, 0) + 1
            w.writerow([status, d, fn, rel])
            if status in ("orphaned", "trashed"):
                dst = os.path.join(OUT, status, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(local, dst)

    print(f"{len(local_files)} thumbnail files pulled, {len(assets)} asset rows")
    for k, v in sorted(counts.items()):
        print(f"{k}: {v}")
    print(f"Report: {report}")


if __name__ == "__main__":
    asyncio.run(main())
