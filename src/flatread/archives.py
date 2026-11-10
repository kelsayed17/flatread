"""A ZIP body's members: every one, or the one a layout's ``member`` names.

Philadelphia Fed, Treasury TIC, CFTC, FINRA and SEC each serve zips of files, so a layout of any format reads a member
by setting ``member``: a name, a regex the whole name matches, or a callable. The stdlib's ``zipfile`` reads stored and
deflated members, which every provider in the inventory writes.
"""

import io
import re
import zipfile
from collections.abc import Callable, Sequence

from flatread.errors import CorruptArchiveError, UnknownLayoutError

__all__ = ["Member", "pick", "read_member", "unzip"]

type Member = str | re.Pattern[str] | Callable[[str], bool]


def _open(body: bytes) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(io.BytesIO(body))
    except (zipfile.BadZipFile, ValueError) as exc:
        msg = f"the body is not a ZIP archive that can be read: {exc}"
        raise CorruptArchiveError(msg) from exc


def _read(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        return archive.read(name)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, ValueError, EOFError) as exc:
        msg = f"the archive's member {name!r} cannot be read: {exc}"
        raise CorruptArchiveError(msg) from exc


def unzip(body: bytes) -> dict[str, bytes]:
    """Return every file of the ZIP *body* by name, in the archive's order; directories are left out."""
    with _open(body) as archive:
        return {info.filename: _read(archive, info.filename) for info in archive.infolist() if not info.is_dir()}


def pick(names: Sequence[str], member: Member) -> str:
    """Return the one of *names* that *member* names; none, or more than one, raises naming the members."""
    if isinstance(member, str):
        found = [name for name in names if name == member]
    elif isinstance(member, re.Pattern):
        found = [name for name in names if member.fullmatch(name)]
    else:
        found = [name for name in names if member(name)]
    if len(found) != 1:
        which = "no member" if not found else f"{len(found)} members"
        msg = f"{which} of the archive match {member!r}: it holds {list(names)!r}"
        raise UnknownLayoutError(msg)
    return found[0]


def read_member(body: bytes, member: Member) -> bytes:
    """Return the file *member* names in the ZIP *body*, inflating only that one."""
    with _open(body) as archive:
        names = [info.filename for info in archive.infolist() if not info.is_dir()]
        return _read(archive, pick(names, member))
