"""Single CLI collector per checkout; acquire before creating an OCX instance."""
from pathlib import Path
import os


class CollectorLease:
    def __init__(self, root):
        self.path = Path(root) / "operations_state" / "kiwoom_collector.lock"
        self.stream = None

    def __enter__(self):
        if os.name != "nt":
            raise OSError("Windows collector lease required")
        import msvcrt
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            if stream.seek(0, 2) == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except BaseException:
            stream.close()
            raise RuntimeError("another collector owns this checkout; no OCX login started")
        self.stream = stream
        return self

    def __exit__(self, *exc):
        if self.stream is not None:
            import msvcrt
            try:
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self.stream.close()
                self.stream = None
