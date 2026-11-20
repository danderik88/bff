"""Sliced files on disk, not in the SQLite BLOB table. Stdlib only.

Files are named `<file_id>.gcode.3mf` in one folder. The folder has a total
size cap: `prune` walks oldest first and deletes files not in `keep` until
the folder fits under the cap. A temp file + `os.replace` makes `save`
atomic, so a hard kill never leaves a half-written file.
"""
import os

DEFAULT_CAP = 500 * 1024 * 1024

FOLDER = None
CAP = DEFAULT_CAP


def setup(folder, cap_bytes=DEFAULT_CAP):
    global FOLDER, CAP
    FOLDER = folder
    CAP = cap_bytes
    os.makedirs(FOLDER, exist_ok=True)


def _path(file_id):
    return os.path.join(FOLDER, f"{file_id}.gcode.3mf")


def save(file_id, data):
    dst = _path(file_id)
    tmp = f"{dst}.tmp{os.getpid()}"
    try:
        with open(tmp, "wb") as out:
            out.write(data)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def exists(file_id):
    return os.path.exists(_path(file_id))


def read(file_id):
    with open(_path(file_id), "rb") as f:
        return f.read()


def remove(file_id):
    try:
        os.remove(_path(file_id))
    except FileNotFoundError:
        pass


def prune(ids_newest_first, keep):
    deleted = []
    entries = [(fid, os.path.getsize(_path(fid))) for fid in reversed(ids_newest_first)
               if os.path.exists(_path(fid))]
    total = sum(size for _, size in entries)
    for fid, size in entries:
        if total <= CAP:
            break
        if fid in keep:
            continue
        os.remove(_path(fid))
        deleted.append(fid)
        total -= size
    return deleted
