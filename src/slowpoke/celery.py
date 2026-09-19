"""Celery integration: one trace per task a worker runs.

    # wherever the Celery app is created
    import slowpoke.celery

    slowpoke.celery.install()

A task dispatched from a request is not traced there: it is traced where it runs, in the worker.
"""

import slowpoke

# Bounded on purpose: a worker that somehow never sees the end of its tasks must not grow a map
# forever. Past this many open tasks the next ones are simply not traced.
MAX_OPEN = 512

_open = {}
_state = {"installed": False}


def install():
    """Connects the worker signals. Idempotent, and never raises: a monitoring package must not be
    able to stop a worker from starting."""
    try:
        if _state["installed"]:
            return True
        from celery import signals

        signals.task_prerun.connect(_prerun, dispatch_uid="slowpoke")
        signals.task_postrun.connect(_postrun, dispatch_uid="slowpoke")
        _state["installed"] = True
        return True
    except Exception:
        return False


def _prerun(task_id=None, task=None, **kwargs):
    try:
        tracer = slowpoke.get_tracer()
        if tracer is None or task_id is None or len(_open) >= MAX_OPEN:
            return
        trace = tracer.start_job(getattr(task, "name", None) or "task", _queue(task))
        if trace is not None:
            _open[task_id] = trace
    except Exception:
        pass  # never let observability break the worker


def _postrun(task_id=None, state=None, **kwargs):
    try:
        trace = _open.pop(task_id, None)
        if trace is None:
            return
        tracer = slowpoke.get_tracer()
        if tracer is not None:
            # RETRY is this attempt giving up: to whoever is waiting for the work it is a failure.
            tracer.finish_job(trace, failed=str(state) in ("FAILURE", "RETRY"))
    except Exception:
        pass


def _queue(task):
    try:
        info = getattr(getattr(task, "request", None), "delivery_info", None) or {}
        return info.get("routing_key") or None
    except Exception:
        return None
