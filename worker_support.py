"""Process exclusion and persistent component status."""
import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def process_lock(data,name):
    path=Path(data)/'Runtime'/ (name+'.lock')
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with path.open('a') as handle:
        try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise RuntimeError('Another '+name+' worker is already running') from None
        yield


def heartbeat(db,name,status):
    with db.transaction() as conn:
        conn.execute('INSERT OR REPLACE INTO runtime_status VALUES(?,?,?)',
                     (name,json.dumps(dict(status,pid=os.getpid())),time.time()))


@contextmanager
def process_mutex(data,name):
    """Blocking transaction-level file mutex shared by API and capture processes."""
    import hashlib
    path=Path(data)/'Runtime'/('Mutex-'+hashlib.sha256(name.encode()).hexdigest()+'.lock')
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with path.open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        try:yield
        finally:fcntl.flock(handle,fcntl.LOCK_UN)
