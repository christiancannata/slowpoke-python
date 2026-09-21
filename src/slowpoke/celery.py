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


def _prerun(task_id=None, task=None, args=None, kwargs=None, **_):
    try:
        tracer = slowpoke.get_tracer()
        if tracer is None or task_id is None or len(_open) >= MAX_OPEN:
            return
        trace = tracer.start_job(_name(task, args, kwargs), _queue(task))
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


# task.map(), .starmap() and .chunks() run as these built-ins, which call the real task in-process
# for every item: the work is that task's, so the Jobs page names it, with how it was batched.
# The other built-ins (backend_cleanup, chord_unlock, accumulate, group, chord) are Celery's own
# work under a clear name and are reported as they are.
_BATCHES = {"celery.map": "map", "celery.starmap": "starmap", "celery.chunks": "chunks"}


def _name(task, args, kwargs):
    """The task name, or for a batch built-in the name of the task it runs. Only the name is read:
    the items are arguments, and arguments never leave the machine."""
    name = getattr(task, "name", None) or "task"
    how = _BATCHES.get(name)
    if how is None:
        return name
    try:
        inner = kwargs.get("task") if isinstance(kwargs, dict) else None
        if inner is None and args:
            inner = args[0]
        inner = inner.get("task") if isinstance(inner, dict) else None
        if isinstance(inner, str) and 0 < len(inner) <= 200 and not any(c.isspace() for c in inner):
            return "%s (%s)" % (inner, how)
    except Exception:
        pass
    return name


def _queue(task):
    try:
        info = getattr(getattr(task, "request", None), "delivery_info", None) or {}
        return info.get("routing_key") or None
    except Exception:
        return None
