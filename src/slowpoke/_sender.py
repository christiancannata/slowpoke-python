import atexit
import http.client
import ipaddress
import os
import queue
import threading
import time
import weakref
from urllib.parse import urlsplit

_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa")


def _private_host(host):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        host = host.lower()
        # single-label names are Docker services or /etc/hosts entries
        return host == "localhost" or "." not in host or host.endswith(_PRIVATE_SUFFIXES)
    return ip.is_private or ip.is_loopback or ip.is_link_local


class HttpSender:
    """Posts OTLP/JSON to the Slowpoke agent on this machine or the private network, with a hard time
    budget shared by connect, write and read. Only the background worker calls it."""

    def __init__(self, host, port, path, timeout):
        self.host = host
        self.port = port
        self.path = path
        self.timeout = timeout

    @classmethod
    def from_url(cls, url, timeout):
        """None unless the URL is plain http to a local or private host: queries must not travel the internet."""
        try:
            parts = urlsplit(url or "")
            if parts.scheme != "http" or not parts.hostname or not _private_host(parts.hostname):
                return None
            port = parts.port or 80
        except ValueError:
            return None
        path = parts.path if parts.path not in ("", "/") else "/v1/traces"
        return cls(parts.hostname, port, path, max(0.001, float(timeout)))

    def send(self, body):
        deadline = time.monotonic() + self.timeout
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        try:
            conn.connect()
            conn.sock.settimeout(max(0.001, deadline - time.monotonic()))
            conn.request("POST", self.path, body, {
                "Content-Type": "application/json",
                "Connection": "close",
                "User-Agent": "slowpoke-python",
            })
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            conn.sock.settimeout(left)
            response = conn.getresponse()
            return 200 <= response.status < 300
        except Exception:
            return False
        finally:
            try:
                conn.close()
            except Exception:
                pass


_instances = weakref.WeakSet()


class BackgroundSender:
    """A bounded queue and one daemon thread: the request only pays for put_nowait. A full queue means
    the agent is slow or gone, and the trace is dropped instead of piling up in the app's memory."""

    def __init__(self, sender, encode, maxsize=256):
        self.sender = sender
        self.encode = encode
        self.maxsize = maxsize
        self._lock = threading.Lock()
        self._after_fork()
        _instances.add(self)

    def _after_fork(self):
        # A forked worker (gunicorn --preload, Celery prefork) inherits the object but not the thread.
        self._queue = queue.Queue(self.maxsize)
        self._thread = None
        self._pid = os.getpid()

    def submit(self, item):
        try:
            if self._thread is None or self._pid != os.getpid():
                self._start()
            self._queue.put_nowait(item)
            return True
        except Exception:
            return False

    def _start(self):
        with self._lock:
            if self._pid != os.getpid():
                self._after_fork()
            if self._thread is None:
                thread = threading.Thread(target=self._run, args=(self._queue,), name="slowpoke-sender", daemon=True)
                thread.start()
                self._thread = thread

    def _run(self, q):
        while True:
            item = q.get()
            try:
                self.sender.send(self.encode(item))
            except Exception:
                pass  # the agent is optional: a broken one costs a trace, nothing else
            finally:
                q.task_done()

    def drain(self, timeout):
        """Waits until every queued trace was handed to the agent (or given up). For tests and exit."""
        deadline = time.monotonic() + timeout
        q = self._queue
        with q.all_tasks_done:
            while q.unfinished_tasks:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                q.all_tasks_done.wait(left)
        return True


@atexit.register
def _drain_at_exit():
    # Scripts and short jobs end right after their last trace: give it a moment, never more.
    deadline = time.monotonic() + 0.5
    for instance in list(_instances):
        if instance._thread is not None and instance._pid == os.getpid():
            instance.drain(max(0.0, deadline - time.monotonic()))


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=lambda: [i._after_fork() for i in list(_instances)])
