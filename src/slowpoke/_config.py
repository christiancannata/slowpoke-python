import os

DEFAULT_ENDPOINT = "http://127.0.0.1:4318/v1/traces"


def _number(raw, default, cast, minimum):
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= minimum else default


class Config:
    """Settings from SLOWPOKE_* environment variables, like the Laravel package. Nonsense values fall
    back to the defaults: a typo in an env file must never stop the app from booting."""

    def __init__(self, enabled=True, endpoint=DEFAULT_ENDPOINT, timeout=0.1, service=None, max_queries=500,
                 max_sql_length=10000, backtrace_limit=100, code_root=None, queue_size=256, http_client=True,
                 max_http_calls=200):
        self.enabled = enabled
        self.endpoint = endpoint
        self.timeout = timeout
        self.service = service
        self.max_queries = max_queries
        self.max_sql_length = max_sql_length
        self.backtrace_limit = backtrace_limit
        self.code_root = code_root
        self.queue_size = queue_size
        self.http_client = http_client
        self.max_http_calls = max_http_calls

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        d = cls()
        enabled = env.get("SLOWPOKE_ENABLED", "").strip().lower()
        return cls(
            enabled=enabled not in ("0", "false", "no", "off"),
            endpoint=env.get("SLOWPOKE_OTLP_ENDPOINT", "").strip() or d.endpoint,
            timeout=_number(env.get("SLOWPOKE_TIMEOUT"), d.timeout, float, 0.001),
            service=env.get("SLOWPOKE_SERVICE", "").strip() or None,
            max_queries=_number(env.get("SLOWPOKE_MAX_QUERIES"), d.max_queries, int, 0),
            max_sql_length=_number(env.get("SLOWPOKE_MAX_SQL_LENGTH"), d.max_sql_length, int, 1),
            backtrace_limit=_number(env.get("SLOWPOKE_BACKTRACE_LIMIT"), d.backtrace_limit, int, 1),
            code_root=env.get("SLOWPOKE_CODE_ROOT", "").strip() or None,
            queue_size=_number(env.get("SLOWPOKE_QUEUE_SIZE"), d.queue_size, int, 1),
            http_client=env.get("SLOWPOKE_HTTP_CLIENT", "").strip().lower() not in ("0", "false", "no", "off"),
            max_http_calls=_number(env.get("SLOWPOKE_MAX_HTTP_CALLS"), d.max_http_calls, int, 0),
        )
