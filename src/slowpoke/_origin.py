import os
import sys
import sysconfig
import time

_PACKAGE_DIR = os.path.dirname(os.path.abspath(__file__)) + os.sep
_THIRD_PARTY_PARTS = ("/site-packages/", "/dist-packages/", "/node_modules/", "/vendor/")


def _stdlib_dirs():
    dirs = set()
    for key in ("stdlib", "platstdlib"):
        try:
            path = sysconfig.get_paths().get(key)
        except Exception:
            path = None
        if path:
            dirs.add(os.path.join(os.path.realpath(path), ""))
            dirs.add(os.path.join(os.path.abspath(path), ""))
    return tuple(dirs)


class OriginFinder:
    """Finds the application line that issued a query. OpenTelemetry's instrumentations point into
    Django or SQLAlchemy, which tells nobody what to fix: the answer is the first frame of the app's own
    code, outside site-packages, the standard library and this package."""

    def __init__(self, code_root, limit=100, cache_size=2048):
        self.root = os.path.join(os.path.abspath(code_root), "")
        self.limit = max(1, int(limit))
        self._skip = _stdlib_dirs() + (_PACKAGE_DIR,)
        self._cache = {}
        self._cache_size = cache_size

    def find(self, task=None):
        """(relative file, line) of the innermost application frame, or None. `task` is the asyncio task
        that opened the trace, for statements run on a worker thread on its behalf."""
        frame = sys._getframe(1)
        origin, budget = self._walk(frame, self.limit)
        if origin is None and budget > 0:
            # SQLAlchemy's asyncio layer runs the statement in a greenlet: the coroutine that awaited
            # it is on the stack of the parent greenlet, suspended where it switched.
            parent = _greenlet_parent_frame()
            if parent is not None:
                origin, budget = self._walk(parent, budget)
        if origin is None and budget > 0 and task is not None:
            # Django's async ORM and sync_to_async run the statement in a thread: the view is a
            # coroutine suspended on "await", reachable from the task that opened the trace.
            origin = self._from_task(task, budget)
        return origin

    def _from_task(self, task, budget):
        try:
            frames = []
            awaitable = task.get_coro()
            if getattr(awaitable, "cr_running", False):
                # Running, not suspended: its await chain is empty and would name the outermost
                # coroutine, a wrong answer. On the loop's own thread it cannot be suspended while we
                # run; on a worker thread it is a few instructions away from its "await": give it at
                # most a couple of milliseconds, then no origin rather than a misleading one.
                asyncio = sys.modules.get("asyncio")
                if asyncio is None or asyncio._get_running_loop() is not None:
                    return None
                deadline = time.monotonic() + 0.002
                while awaitable.cr_running:
                    if time.monotonic() > deadline:
                        return None
                    time.sleep(0)
            while awaitable is not None and len(frames) < budget:
                frame = None
                for attr in ("cr_frame", "ag_frame", "gi_frame"):
                    frame = getattr(awaitable, attr, None)
                    if frame is not None:
                        break
                if frame is None:
                    break
                frames.append(frame)
                awaitable = (getattr(awaitable, "cr_await", None) or getattr(awaitable, "ag_await", None)
                             or getattr(awaitable, "gi_yieldfrom", None))
            for frame in reversed(frames):  # innermost first
                relative = self.classify(frame.f_code.co_filename)
                if relative is not None:
                    return relative, frame.f_lineno
        except Exception:
            pass
        return None

    def from_frame(self, frame):
        return self._walk(frame, self.limit)[0]

    def _walk(self, frame, budget):
        while frame is not None and budget > 0:
            budget -= 1
            code = frame.f_code
            relative = self.classify(code.co_filename)
            if relative is not None:
                return (relative, frame.f_lineno), budget
            frame = frame.f_back
        return None, budget

    def classify(self, filename):
        """The path to report for a file, or None for code that is not the application's. Cached per
        file name: a request runs hundreds of queries through the same few dozen files."""
        try:
            return self._cache[filename]
        except KeyError:
            pass
        result = self._classify(filename)
        if len(self._cache) >= self._cache_size:
            self._cache.clear()
        self._cache[filename] = result
        return result

    def _classify(self, filename):
        if not filename or filename.startswith("<"):
            return None  # <frozen ...>, <string>, <stdin>
        path = filename.replace("\\", "/")
        if any(part in path for part in _THIRD_PARTY_PARTS) or path.startswith(("vendor/", "node_modules/")):
            return None
        absolute = os.path.abspath(filename)
        if absolute.startswith(self._skip):
            return None
        if absolute.startswith(self.root):
            return absolute[len(self.root):].replace("\\", "/")
        return path


def _greenlet_parent_frame():
    greenlet = sys.modules.get("greenlet")
    if greenlet is None:
        return None
    try:
        parent = greenlet.getcurrent().parent
        return parent.gr_frame if parent is not None else None
    except Exception:
        return None
