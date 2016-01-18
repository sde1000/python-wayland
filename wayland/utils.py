import os
import tempfile

class NoXDGRuntimeDir(Exception):
    pass

class AnonymousFile(object):
    def __init__(self, size):
        xdg_runtime_dir = os.getenv('XDG_RUNTIME_DIR')
        if not xdg_runtime_dir:
            raise NoXDGRuntimeDir()
        self._fd, name = tempfile.mkstemp(dir=xdg_runtime_dir)
        os.ftruncate(self._fd, size)
    def fileno(self):
        if self._fd:
            return self._fd
        raise OSError
    def close(self):
        if self._fd:
            os.close(self._fd)
            self._fd = None
    def __enter__(self):
        return self._fd
    def __exit__(self, exc_type, exc_value, traceback):
        if self._fd:
            self.close()
