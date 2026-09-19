import socket
import threading
import time

from slowpoke._sender import BackgroundSender, HttpSender


def test_only_plain_http_to_local_or_private_hosts():
    for url in [
        "http://127.0.0.1:4318/v1/traces", "http://localhost:4318/v1/traces", "http://agent:4318/v1/traces",
        "http://10.0.3.7:4318/v1/traces", "http://192.168.1.20:4318/v1/traces", "http://172.17.0.1:4318/v1/traces",
        "http://[::1]:4318/v1/traces", "http://slowpoke.internal:4318/v1/traces", "http://box.local/v1/traces",
    ]:
        assert HttpSender.from_url(url, 0.05) is not None, url
    for url in [
        "https://127.0.0.1:4318/v1/traces", "http://8.8.8.8:4318/v1/traces", "http://collector.example.com/v1/traces",
        "ftp://127.0.0.1/x", "not a url", "", "http://:4318/v1/traces", "http://127.0.0.1:notaport/",
    ]:
        assert HttpSender.from_url(url, 0.05) is None, url


def test_default_port_and_path():
    s = HttpSender.from_url("http://127.0.0.1", 0.05)
    assert (s.host, s.port, s.path) == ("127.0.0.1", 80, "/v1/traces")


def agent(answer=b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}", delay=0.0):
    """A one-request agent on a free port; returns (port, received bytes holder)."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    got = {}

    def serve():
        conn, _ = server.accept()
        conn.settimeout(2)
        data = b""
        while b"\r\n\r\n" not in data or len(data.split(b"\r\n\r\n", 1)[1]) < int(
            data.split(b"Content-Length: ")[1].split(b"\r\n")[0] if b"Content-Length: " in data else 0
        ):
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        got["request"] = data
        time.sleep(delay)
        if answer:
            try:
                conn.sendall(answer)
            except OSError:
                pass
        conn.close()
        server.close()

    threading.Thread(target=serve, daemon=True).start()
    return server.getsockname()[1], got


def test_posts_json_to_the_agent():
    port, got = agent()
    s = HttpSender.from_url("http://127.0.0.1:%d/v1/traces" % port, 1.0)
    assert s.send(b'{"resourceSpans":[]}') is True
    request = got["request"]
    assert request.startswith(b"POST /v1/traces HTTP/1.1\r\n")
    assert b"\r\nContent-Type: application/json\r\n" in request
    assert b"\r\nContent-Length: 20\r\n" in request
    assert request.endswith(b'\r\n\r\n{"resourceSpans":[]}')


def test_a_slow_agent_costs_the_timeout_and_nothing_more():
    port, _ = agent(delay=2.0)
    s = HttpSender.from_url("http://127.0.0.1:%d/v1/traces" % port, 0.1)
    started = time.monotonic()
    assert s.send(b"{}") is False
    assert time.monotonic() - started < 0.5


def test_error_status_is_false():
    port, _ = agent(answer=b"HTTP/1.1 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n")
    assert HttpSender.from_url("http://127.0.0.1:%d/v1/traces" % port, 1.0).send(b"{}") is False


def test_closed_port_and_unknown_host_do_not_raise():
    started = time.monotonic()
    assert HttpSender.from_url("http://127.0.0.1:1/v1/traces", 0.05).send(b"{}") is False
    assert time.monotonic() - started < 0.5
    assert HttpSender.from_url("http://slowpoke-agent-that-does-not-exist:4318/v1/traces", 0.05).send(b"{}") is False


class Blocking:
    def __init__(self):
        self.release = threading.Event()
        self.sent = []

    def send(self, body):
        self.release.wait(5)
        self.sent.append(body)
        return True


def test_submit_never_waits_for_the_agent():
    blocking = Blocking()
    bg = BackgroundSender(blocking, encode=lambda item: item, maxsize=2)
    started = time.monotonic()
    results = [bg.submit(b"%d" % i) for i in range(10)]
    assert time.monotonic() - started < 0.05
    # One trace in the sender's hands, two queued, the rest dropped.
    assert results.count(True) <= 3 and results[-1] is False
    blocking.release.set()
    assert bg.drain(2.0)
    assert blocking.sent[0] == b"0"
    assert len(blocking.sent) == results.count(True)


def test_worker_survives_broken_encoders_and_senders():
    sent = []

    class Flaky:
        def send(self, body):
            if body == b"boom":
                raise RuntimeError("agent exploded")
            sent.append(body)
            return True

    def encode(item):
        if item == "bad":
            raise ValueError("cannot encode")
        return item

    bg = BackgroundSender(Flaky(), encode=encode, maxsize=10)
    for item in ["bad", b"boom", b"ok"]:
        assert bg.submit(item)
    assert bg.drain(2.0)
    assert sent == [b"ok"]


def test_worker_restarts_after_fork_like_reset():
    sent = []

    class Collect:
        def send(self, body):
            sent.append(body)
            return True

    bg = BackgroundSender(Collect(), encode=lambda x: x, maxsize=10)
    bg.submit(b"a")
    assert bg.drain(2.0)
    bg._after_fork()  # a forked child has the object but not the thread
    bg.submit(b"b")
    assert bg.drain(2.0)
    assert sent == [b"a", b"b"]
