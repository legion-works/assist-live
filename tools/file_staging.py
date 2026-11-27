import os
from collections.abc import Iterator
from contextlib import contextmanager
from io import BufferedRandom
from pathlib import Path
from tempfile import mkstemp


@contextmanager
def stage_file(path: Path) -> Iterator[BufferedRandom]:
    descriptor, filename = mkstemp(dir=path.parent, prefix=".stage-")
    name = Path(filename)
    try:
        with open(descriptor, "w+b") as stream:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        name.replace(path)
    finally:
        name.unlink(missing_ok=True)
