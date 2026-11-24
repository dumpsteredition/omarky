"""Short-lived, owner-private cards for explicit manual chat handoffs.

Each card contains only the exact marked PNG and the note typed for it. Cards
are addressed by an unpredictable ID, never by a caller-supplied path.
"""
from __future__ import annotations

import json
import math
import os
import re
import secrets
import stat
import time
from pathlib import Path
from typing import Any

CARD_TTL_SECONDS = 30 * 60
MAX_PNG_BYTES = 12 * 1024 * 1024
MAX_NOTE_BYTES = 16 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_CARD_ID = re.compile(r"\A[0-9a-f]{32}\Z")
_META_NAME = "metadata.json"
_PNG_NAME = "marked.png"
_NOTE_NAME = "note.txt"
_META_LIMIT = 4096


def _validate_id(card_id: str) -> str:
    if not isinstance(card_id, str) or _CARD_ID.fullmatch(card_id) is None:
        raise ValueError("card ID must be exactly 32 lowercase hexadecimal characters")
    return card_id


def _lstat_directory(path: Path) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise OSError(f"managed card path is not a real directory: {path.name}")
    if info.st_uid != os.getuid():
        raise PermissionError(f"managed card directory is not owned by this user: {path.name}")
    return info


def _ensure_directory(path: Path, *, create: bool, private: bool) -> bool:
    info = _lstat_directory(path)
    created = False
    if info is None:
        if not create:
            return False
        try:
            path.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        info = _lstat_directory(path)
        if info is None:
            raise OSError("could not create the private card directory")
    if (private or created) and stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(path, 0o700, follow_symlinks=False)
        info = _lstat_directory(path)
        if info is None or stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError("could not restrict the private card directory to mode 0700")
    return True


def _managed_root(*, create: bool) -> Path | None:
    runtime_value = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_value:
        runtime_path = Path(runtime_value)
        if not runtime_path.is_absolute():
            raise ValueError("XDG_RUNTIME_DIR must be an absolute path")
        runtime = runtime_path.resolve(strict=True)
        info = runtime.stat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise PermissionError("XDG_RUNTIME_DIR must be a private directory owned by this user")
        parent = runtime
    else:
        home = Path.home().resolve(strict=True)
        home_info = home.stat()
        if not stat.S_ISDIR(home_info.st_mode) or home_info.st_uid != os.getuid():
            raise PermissionError("the home directory is not owned by this user")
        cache = home / ".cache"
        if not _ensure_directory(cache, create=create, private=False):
            return None
        parent = cache

    omarky_dir = parent / "omarky"
    if not _ensure_directory(omarky_dir, create=create, private=True):
        return None
    cards_dir = omarky_dir / "cards"
    if not _ensure_directory(cards_dir, create=create, private=True):
        return None
    return cards_dir


def _open_card_dir(root: Path, card_id: str, *, create: bool = False) -> int:
    card_dir = root / card_id
    if create:
        try:
            card_dir.mkdir(mode=0o700)
        except FileExistsError as exc:
            raise FileExistsError("card ID collision") from exc
        os.chmod(card_dir, 0o700, follow_symlinks=False)
    info = _lstat_directory(card_dir)
    if info is None:
        raise FileNotFoundError("unknown annotation card")
    if stat.S_IMODE(info.st_mode) != 0o700:
        raise PermissionError("annotation card directory is not private")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(card_dir, flags)
    opened = os.fstat(descriptor)
    if (not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.getuid()
            or stat.S_IMODE(opened.st_mode) != 0o700):
        os.close(descriptor)
        raise PermissionError("annotation card directory changed while opening")
    return descriptor


def _write_new_file(directory_fd: int, name: str, content: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_fd)
    try:
        os.fchmod(descriptor, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError("card file is not a regular owner-private file")
        pending = memoryview(content)
        while pending:
            written = os.write(descriptor, pending)
            if written <= 0:
                raise OSError("could not finish writing card file")
            pending = pending[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _read_file(directory_fd: int, name: str, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise PermissionError("card file is not a private, single-link regular file")
        if info.st_size > limit:
            raise ValueError("card file exceeds its size limit")
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > limit:
            raise ValueError("card file exceeds its size limit")
        return bytes(data)
    finally:
        os.close(descriptor)


def _created_at(directory_fd: int) -> float:
    raw = _read_file(directory_fd, _META_NAME, _META_LIMIT)
    try:
        metadata: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("annotation card metadata is invalid") from exc
    if not isinstance(metadata, dict) or type(metadata.get("version")) is not int \
            or metadata.get("version") != 1:
        raise ValueError("annotation card metadata is invalid")
    created_at = metadata.get("created_at")
    if (not isinstance(created_at, (int, float)) or isinstance(created_at, bool)
            or not math.isfinite(created_at)):
        raise ValueError("annotation card metadata is invalid")
    return float(created_at)


def _delete_card_dir(root: Path, card_id: str) -> bool:
    card_dir = root / card_id
    info = _lstat_directory(card_dir)
    if info is None:
        return False
    try:
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.chmod(card_dir, 0o700, follow_symlinks=False)
        directory_fd = _open_card_dir(root, card_id)
    except OSError:
        return False
    try:
        for name in (_PNG_NAME, _NOTE_NAME, _META_NAME):
            try:
                entry = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(entry.st_mode) or (stat.S_ISREG(entry.st_mode)
                                                and entry.st_uid == os.getuid()):
                try:
                    os.unlink(name, dir_fd=directory_fd)
                except OSError:
                    return False
    finally:
        os.close(directory_fd)
    try:
        os.rmdir(card_dir)
        return True
    except OSError:
        return False


def _iter_card_ids(root: Path):
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if _CARD_ID.fullmatch(entry.name) is None:
                    continue
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid():
                    yield entry.name
    except OSError:
        return


def _prune_expired(root: Path, *, now: float | None = None) -> None:
    current = time.time() if now is None else now
    for card_id in _iter_card_ids(root):
        try:
            directory_fd = _open_card_dir(root, card_id)
            try:
                created_at = _created_at(directory_fd)
            finally:
                os.close(directory_fd)
        except (OSError, ValueError):
            continue
        if created_at + CARD_TTL_SECONDS <= current:
            _delete_card_dir(root, card_id)


def save_card(png: bytes, note: str) -> dict[str, str]:
    """Save an explicitly marked PNG and its exact note in a private TTL card."""
    if not isinstance(png, bytes) or not png.startswith(PNG_SIGNATURE):
        raise ValueError("marked image must be PNG bytes")
    if len(png) > MAX_PNG_BYTES:
        raise ValueError("marked PNG exceeds the 12 MiB card limit")
    if not isinstance(note, str):
        raise TypeError("note must be text")
    try:
        note_bytes = note.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise ValueError("note must be valid UTF-8 text") from exc
    if len(note_bytes) > MAX_NOTE_BYTES:
        raise ValueError("note exceeds the 16 KiB card limit")

    root = _managed_root(create=True)
    if root is None:
        raise OSError("no private runtime directory is available for annotation cards")
    created_at = time.time()
    _prune_expired(root, now=created_at)
    metadata = json.dumps(
        {"version": 1, "created_at": created_at},
        separators=(",", ":"), allow_nan=False,
    ).encode("ascii")

    for _attempt in range(5):
        card_id = secrets.token_hex(16)
        try:
            directory_fd = _open_card_dir(root, card_id, create=True)
        except FileExistsError:
            continue
        card_dir = root / card_id
        try:
            _write_new_file(directory_fd, _PNG_NAME, png)
            _write_new_file(directory_fd, _NOTE_NAME, note_bytes)
            _write_new_file(directory_fd, _META_NAME, metadata)
            os.fsync(directory_fd)
        except Exception:
            os.close(directory_fd)
            _delete_card_dir(root, card_id)
            raise
        else:
            os.close(directory_fd)
        return {
            "id": card_id,
            "image_path": str(card_dir / _PNG_NAME),
            "note": note,
        }
    raise OSError("could not allocate a unique annotation card ID")


def load_card(card_id: str) -> tuple[bytes, str]:
    """Return the exact PNG and literal note for one valid, unexpired card ID."""
    card_id = _validate_id(card_id)
    root = _managed_root(create=False)
    if root is None:
        raise ValueError("unknown or expired annotation card")
    now = time.time()
    _prune_expired(root, now=now)
    try:
        directory_fd = _open_card_dir(root, card_id)
    except FileNotFoundError as exc:
        raise ValueError("unknown or expired annotation card") from exc
    try:
        created_at = _created_at(directory_fd)
        if created_at > now + 60:
            raise ValueError("annotation card metadata is invalid")
        if created_at + CARD_TTL_SECONDS <= now:
            raise ValueError("annotation card has expired")
        png = _read_file(directory_fd, _PNG_NAME, MAX_PNG_BYTES)
        if not png.startswith(PNG_SIGNATURE):
            raise ValueError("annotation card PNG is invalid")
        note_bytes = _read_file(directory_fd, _NOTE_NAME, MAX_NOTE_BYTES)
        try:
            note = note_bytes.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise ValueError("annotation card note is invalid UTF-8") from exc
        return png, note
    finally:
        os.close(directory_fd)


def forget_card(card_id: str) -> bool:
    """Remove one managed card by its exact ID, without accepting arbitrary paths."""
    card_id = _validate_id(card_id)
    root = _managed_root(create=False)
    if root is None:
        return False
    _prune_expired(root)
    return _delete_card_dir(root, card_id)


def forget_all() -> int:
    """Remove all valid-ID card directories owned by this user; leave other files alone."""
    root = _managed_root(create=False)
    if root is None:
        return 0
    removed = 0
    for card_id in list(_iter_card_ids(root)):
        if _delete_card_dir(root, card_id):
            removed += 1
    return removed
