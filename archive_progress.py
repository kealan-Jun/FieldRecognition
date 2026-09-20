"""Main-writer progress, never a timer that masks a blocked NAS operation."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_observer = ContextVar('archive_progress', default=None)


@contextmanager
def observe(callback, interval=5):
    state = {'callback': callback, 'interval': interval, 'last': time.monotonic(), 'details': {}}
    token = _observer.set(state)
    try:
        yield
    finally:
        _observer.reset(token)


def advance(phase=None, completed=None, total=None):
    state = _observer.get()
    if state is None:
        return
    if phase is not None:
        state['details'] = {'work_phase': phase, 'progress_current': completed, 'progress_total': total}
    now = time.monotonic()
    if now - state['last'] >= state['interval']:
        state['callback'](dict(state['details']))
        state['last'] = now
