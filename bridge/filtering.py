"""Trusted scripts may delete files only; surviving bytes and modes are verified."""
import os
from pathlib import Path
import stat
import subprocess
import tempfile
from .git import BridgeError


def projection(git, files, script):
    with tempfile.TemporaryDirectory(prefix='bridge-filter-') as d:
        git.materialize(files, d)
        p = subprocess.run(['bash', str(script), d], cwd=script.parent,
                           capture_output=True)
        if p.returncode:
            raise BridgeError('Sanitizer failed (exit %s): %s' % (
                p.returncode, p.stderr.decode(errors='replace')[-2000:]))
        remaining = {}
        for directory, dirs, names in os.walk(d, followlinks=False):
            for name in list(dirs):
                if (Path(directory) / name).is_symlink():
                    dirs.remove(name)
                    names.append(name)
            for name in names:
                target = Path(directory) / name
                path = str(target.relative_to(d))
                st = target.lstat()
                if stat.S_ISLNK(st.st_mode):
                    mode, content = '120000', os.fsencode(os.readlink(target))
                elif stat.S_ISREG(st.st_mode):
                    mode = '100755' if st.st_mode & 0o111 else '100644'
                    content = target.read_bytes()
                else:
                    raise BridgeError('Sanitizer created an unsupported file: ' + path)
                entry = (mode, git.blob(content))
                if files.get(path) != entry:
                    raise BridgeError('Sanitizer must only remove whole files: ' + repr(path))
                remaining[path] = entry
        return remaining


def overlay(destination, dest_projection, source_projection):
    result = dict(destination)
    for path in dest_projection:
        result.pop(path, None)
    for path, value in source_projection.items():
        if path in destination and path not in dest_projection and destination[path] != value:
            raise BridgeError('Source would overwrite an excluded destination path: ' + repr(path))
        result[path] = value
    # A file/directory transition must never delete an excluded descendant or ancestor.
    for path in result:
        parts = path.split('/')
        for i in range(1, len(parts)):
            if '/'.join(parts[:i]) in result:
                raise BridgeError('File/directory collision with a preserved path: ' + repr(path))
    return result
