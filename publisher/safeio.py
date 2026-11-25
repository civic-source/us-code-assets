"""Descriptor-pinned bounded writes and files-only USTAR extraction."""
import zlib
import hashlib
import os
from pathlib import Path
import stat
import tarfile
from publisher.protocol import safe_path

RESERVE_BYTES = 2 * 1024 ** 3
RESERVE_INODES = 10000
FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
CHUNK = 65536


def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_nlink)


def directory(path):
    current = os.open('/', FLAGS | os.O_DIRECTORY)
    try:
        for part in Path(os.path.abspath(path)).parts[1:]:
            child = os.open(part, FLAGS | os.O_DIRECTORY, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def capacity(fd, needed_bytes=0, needed_inodes=0):
    value = os.fstatvfs(fd)
    unit = value.f_frsize or value.f_bsize
    rounded = ((needed_bytes + unit - 1) // unit) * unit
    if value.f_bavail * unit - RESERVE_BYTES < rounded or value.f_favail - RESERVE_INODES < needed_inodes:
        raise ValueError('Insufficient free bytes/inodes for publication reserve')


def fresh_directory(path, mode=0o700):
    parent = directory(Path(path).parent)
    try:
        capacity(parent, 4096, 1)
        os.mkdir(Path(path).name, mode, dir_fd=parent)
        created = os.open(Path(path).name, FLAGS | os.O_DIRECTORY, dir_fd=parent)
        try:
            os.fchmod(created, mode)
            os.fsync(created)
        finally:
            os.close(created)
        os.fsync(parent)
    finally:
        os.close(parent)


def parents(root, path):
    current = os.dup(root)
    try:
        for part in safe_path(path).split('/')[:-1]:
            capacity(current, 1, 1)
            try:
                os.mkdir(part, 0o755, dir_fd=current)
                owned = os.open(part, FLAGS | os.O_DIRECTORY, dir_fd=current)
                try:
                    os.fchmod(owned, 0o755)
                    os.fsync(owned)
                finally:
                    os.close(owned)
                os.fsync(current)
            except FileExistsError:
                pass
            child = os.open(part, FLAGS | os.O_DIRECTORY, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def read_exact(stream, length):
    chunks = []
    remaining = length
    while remaining:
        data = stream.read(min(CHUNK, remaining))
        if not data:
            raise ValueError('Truncated archive transport')
        chunks.append(data)
        remaining -= len(data)
    return b''.join(chunks)


def write_all(fd, data, parent):
    capacity(parent, len(data), 0)
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise ValueError('Publication write made no progress')
        view = view[count:]


def regular_file(path):
    parent = directory(Path(path).parent)
    try:
        fd = os.open(Path(path).name, FLAGS, dir_fd=parent)
    finally:
        os.close(parent)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(fd)
        raise ValueError('Transport must be a regular single-link file')
    return fd


def archive_digest(fd, expected):
    before = os.fstat(fd)
    if before.st_size > 1_000_000_000:
        raise ValueError('Compressed archive exceeds its bound')
    digest = hashlib.sha256()
    length = 0
    while data := os.read(fd, CHUNK):
        length += len(data)
        if length > before.st_size:
            raise ValueError('Archive grew during bounded digest read')
        digest.update(data)
    if identity(before) != identity(os.fstat(fd)) or digest.hexdigest() != expected:
        raise ValueError('Archive identity or digest mismatch')
    os.lseek(fd, 0, os.SEEK_SET)
    return before


def header_info(header):
    if header[257:265] != b'ustar\x0000':
        raise ValueError('Only bounded files-only USTAR is supported')
    info = tarfile.TarInfo.frombuf(header, 'utf-8', 'strict')
    if (info.type not in {tarfile.REGTYPE, tarfile.AREGTYPE} or info.linkname
            or info.sparse is not None or info.uid != 0 or info.gid != 0
            or info.mtime != 0 or info.mode != 0o644 or info.uname or info.gname):
        raise ValueError('Noncanonical, linked, sparse or special archive member')
    safe_path(info.name)
    return info


def extract_member(stream, root, info, expected):
    if info.size != expected['byteLength']:
        raise ValueError('Archive member length mismatch')
    parent = parents(root, info.name)
    try:
        capacity(parent, info.size, 1)
        fd = os.open(info.name.split('/')[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent)
        try:
            os.fchmod(fd, 0o644)
            digest = hashlib.sha256()
            remaining = info.size
            while remaining:
                data = read_exact(stream, min(CHUNK, remaining))
                write_all(fd, data, parent)
                digest.update(data)
                remaining -= len(data)
            if digest.hexdigest() != expected['sha256'] or os.fstat(fd).st_nlink != 1:
                raise ValueError('Archive member hash/identity mismatch')
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(parent)
    finally:
        os.close(parent)
    if any(read_exact(stream, (-info.size) % 512)):
        raise ValueError('Nonzero TAR padding')


class SingleGzipReader:
    """Bound each expansion read and refuse concatenated gzip members/trailing bytes."""
    def __init__(self, raw):
        self.raw = raw
        self.decoder = zlib.decompressobj(31)
        self.pending = b''
        self.finished = False

    def read(self, maximum):
        output = bytearray()
        while len(output) < maximum and not self.finished:
            if not self.pending:
                self.pending = self.raw.read(CHUNK)
                if not self.pending:
                    raise ValueError('Truncated gzip stream')
            output.extend(self.decoder.decompress(self.pending, maximum - len(output)))
            self.pending = self.decoder.unconsumed_tail
            if self.decoder.eof:
                if self.decoder.unused_data or self.raw.read(1):
                    raise ValueError('Multiple gzip members or trailing compressed bytes')
                self.finished = True
        return bytes(output)


def extract_ustar_fd(fd, root, table, digest):
    before = archive_digest(fd, digest)
    with os.fdopen(os.dup(fd), 'rb') as raw:
        stream = SingleGzipReader(raw)
        seen = set()
        while True:
            header = read_exact(stream, 512)
            if not any(header):
                if any(read_exact(stream, 512)):
                    raise ValueError('Missing second TAR end block')
                tail = stream.read(10241)
                if len(tail) > 10240 or any(tail) or stream.read(1):
                    raise ValueError('Unexpected or oversized TAR trailer')
                break
            info = header_info(header)
            if info.name in seen or info.name not in table:
                raise ValueError('Duplicate or unlisted archive member')
            seen.add(info.name)
            extract_member(stream, root, info, table[info.name])
        if seen != set(table):
            raise ValueError('Missing archive members')
    if identity(before) != identity(os.fstat(fd)):
        raise ValueError('Archive changed during extraction')
    os.fsync(root)


def extract_ustar(path, destination, table, digest):
    fd = regular_file(path)
    try:
        root = directory(destination)
        try:
            extract_ustar_fd(fd, root, table, digest)
        finally:
            os.close(root)
    finally:
        os.close(fd)


def verified_file(fd, row):
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size != row['byteLength']:
        raise ValueError('Staged file identity/size mismatch')
    digest = hashlib.sha256()
    size = 0
    while data := os.read(fd, CHUNK):
        size += len(data)
        if size > row['byteLength']:
            raise ValueError('Staged file grew during closure check')
        digest.update(data)
    if size != row['byteLength'] or digest.hexdigest() != row['sha256'] or identity(before) != identity(os.fstat(fd)):
        raise ValueError('Staged file digest/identity mismatch')


def verify_tree(path, table):
    allowed_directories = set()
    for name in table:
        pieces = name.split('/')[:-1]
        while pieces:
            allowed_directories.add('/'.join(pieces))
            pieces.pop()
    seen = set()
    def visit(fd, prefix):
        before = os.fstat(fd)
        with os.scandir(fd) as entries:
            for entry in entries:
                name = (prefix + '/' if prefix else '') + entry.name
                safe_path(name)
                child = os.open(entry.name, FLAGS, dir_fd=fd)
                try:
                    if stat.S_ISDIR(os.fstat(child).st_mode):
                        if name not in allowed_directories:
                            raise ValueError('Unlisted staged directory')
                        visit(child, name)
                    else:
                        if name not in table or name in seen:
                            raise ValueError('Unexpected staged file')
                        verified_file(child, table[name])
                        seen.add(name)
                finally:
                    os.close(child)
        if identity(before) != identity(os.fstat(fd)):
            raise ValueError('Staged directory changed during closure check')
    root = directory(path)
    try:
        visit(root, '')
        if seen != set(table):
            raise ValueError('Incomplete staged file closure')
    finally:
        os.close(root)
