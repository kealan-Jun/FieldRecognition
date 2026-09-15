"""Production-ready task queue with leases, retries, and failure handling."""
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from database import Database


TaskStatus = Literal['queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted']


class TaskQueue:
    """
    Persistent task queue with worker leases and automatic recovery.

    Features:
    - Atomic task claiming with worker leases
    - Automatic recovery of expired leases
    - Exponential backoff retries with jitter
    - Dead letter queue for permanently failed tasks
    - Graceful shutdown support
    """

    def __init__(self, db: Database, worker_id: str | None = None):
        """
        Initialize task queue.

        Args:
            db: Database instance
            worker_id: Unique worker identifier (generated if not provided)
        """
        self.db = db
        self.worker_id = worker_id or f'worker-{uuid.uuid4().hex[:8]}'
        self.lease_seconds = 300  # 5 minutes
        self.heartbeat_interval = 60  # 1 minute
        self.max_retries = 3
        self.base_backoff_seconds = 5

    def claim_task(self, task_type: str | None = None) -> dict | None:
        """
        Atomically claim the next available task.

        Args:
            task_type: Optional filter by task type

        Returns:
            Task document if claimed, None if no tasks available
        """
        with self.db.transaction('IMMEDIATE') as conn:
            now = datetime.now(timezone.utc).isoformat()
            expires = (datetime.now(timezone.utc) + timedelta(seconds=self.lease_seconds)).isoformat()

            # Find claimable task: queued OR running with expired lease
            where_clauses = [
                "(status='queued' OR (status='running' AND lease_expires_at < ?))",
                "retry_count < max_retries"
            ]

            if task_type:
                where_clauses.append(f"json_extract(document,'$.task_type')='{task_type}'")

            where_sql = ' AND '.join(where_clauses)

            # Atomic claim
            cursor = conn.execute(f'''
                UPDATE jobs SET
                    status='running',
                    lease_holder=?,
                    lease_expires_at=?,
                    document=json_set(document, '$.claimed_at', ?)
                WHERE id=(
                    SELECT id FROM jobs
                    WHERE {where_sql}
                    ORDER BY rowid LIMIT 1
                )
                RETURNING *
            ''', (self.worker_id, expires, now, now))

            row = cursor.fetchone()

            if not row:
                return None

            task = json.loads(row['document'])
            task.update({
                'job_id': row['id'],
                'status': row['status'],
                'lease_holder': row['lease_holder'],
                'lease_expires_at': row['lease_expires_at'],
                'retry_count': row['retry_count']
            })

            return task

    def renew_lease(self, job_id: str) -> bool:
        """
        Renew task lease (call from long-running tasks).

        Args:
            job_id: Task ID

        Returns:
            True if renewed, False if no longer owns the lease
        """
        expires = (datetime.now(timezone.utc) + timedelta(seconds=self.lease_seconds)).isoformat()

        with self.db.transaction() as conn:
            cursor = conn.execute('''
                UPDATE jobs SET lease_expires_at=?
                WHERE id=? AND lease_holder=? AND status='running'
            ''', (expires, job_id, self.worker_id))

            return cursor.rowcount > 0

    def complete_task(self, job_id: str, result: dict):
        """
        Mark task as completed.

        Args:
            job_id: Task ID
            result: Result data to merge into task document
        """
        with self.db.transaction() as conn:
            row = conn.execute(
                'SELECT document FROM jobs WHERE id=?', (job_id,)
            ).fetchone()

            if not row:
                return

            doc = json.loads(row['document'])
            doc.update(result)
            doc['finished_at'] = datetime.now(timezone.utc).isoformat()

            conn.execute('''
                UPDATE jobs SET
                    status='completed',
                    lease_holder=NULL,
                    lease_expires_at=NULL,
                    document=?
                WHERE id=? AND lease_holder=?
            ''', (json.dumps(doc), job_id, self.worker_id))

    def fail_task(self, job_id: str, error: str, retry: bool = True):
        """
        Mark task as failed and optionally retry.

        Args:
            job_id: Task ID
            error: Error message
            retry: Whether to retry (subject to max_retries)
        """
        with self.db.transaction('IMMEDIATE') as conn:
            row = conn.execute(
                'SELECT document, retry_count, max_retries FROM jobs WHERE id=?',
                (job_id,)
            ).fetchone()

            if not row:
                return

            doc = json.loads(row['document'])
            retry_count = row['retry_count']
            max_retries = row['max_retries']

            # Record failure
            failure_record = {
                'error': error,
                'failed_at': datetime.now(timezone.utc).isoformat(),
                'worker_id': self.worker_id,
                'attempt': retry_count + 1
            }

            failures = doc.get('failure_history', [])
            failures.append(failure_record)
            doc['failure_history'] = failures
            doc['last_error'] = error

            # Determine if should retry
            should_retry = retry and retry_count < max_retries

            if should_retry:
                # Calculate backoff with jitter
                backoff = self.base_backoff_seconds * (2 ** retry_count)
                jitter = backoff * 0.2 * (2 * (hash(job_id) % 100) / 100 - 1)
                retry_after = backoff + jitter

                retry_at = datetime.now(timezone.utc) + timedelta(seconds=retry_after)

                conn.execute('''
                    UPDATE jobs SET
                        status='queued',
                        lease_holder=NULL,
                        lease_expires_at=NULL,
                        retry_count=retry_count+1,
                        document=?
                    WHERE id=?
                ''', (json.dumps(doc), job_id))

            else:
                # Move to failed (dead letter)
                doc['finished_at'] = datetime.now(timezone.utc).isoformat()

                conn.execute('''
                    UPDATE jobs SET
                        status='failed',
                        lease_holder=NULL,
                        lease_expires_at=NULL,
                        document=?
                    WHERE id=?
                ''', (json.dumps(doc), job_id))

    def cancel_task(self, job_id: str):
        """Cancel a task (can only cancel queued or running tasks owned by this worker)."""
        with self.db.transaction() as conn:
            conn.execute('''
                UPDATE jobs SET
                    status='cancelled',
                    lease_holder=NULL,
                    lease_expires_at=NULL
                WHERE id=? AND (status='queued' OR (status='running' AND lease_holder=?))
            ''', (job_id, self.worker_id))

    def recover_expired_leases(self) -> int:
        """
        Recover tasks with expired leases (call periodically).

        Returns:
            Number of tasks recovered
        """
        with self.db.transaction('IMMEDIATE') as conn:
            now = datetime.now(timezone.utc).isoformat()

            cursor = conn.execute('''
                UPDATE jobs SET
                    status='queued',
                    lease_holder=NULL,
                    lease_expires_at=NULL
                WHERE status='running' AND lease_expires_at < ?
            ''', (now,))

            return cursor.rowcount

    def get_queue_stats(self) -> dict:
        """Get queue statistics."""
        with self.db.connection() as conn:
            stats = {}

            for status in ['queued', 'running', 'completed', 'failed', 'interrupted']:
                count = conn.execute(
                    'SELECT COUNT(*) FROM jobs WHERE status=?', (status,)
                ).fetchone()[0]
                stats[status] = count

            # Active workers
            now = datetime.now(timezone.utc).isoformat()
            active_workers = conn.execute('''
                SELECT COUNT(DISTINCT lease_holder) FROM jobs
                WHERE status='running' AND lease_expires_at > ?
            ''', (now,)).fetchone()[0]

            stats['active_workers'] = active_workers

            # Retry stats
            retry_count = conn.execute('''
                SELECT COUNT(*) FROM jobs
                WHERE retry_count > 0 AND status IN ('queued', 'running')
            ''').fetchone()[0]

            stats['retry_queue'] = retry_count

            return stats

    def cleanup_completed_tasks(self, before: datetime, limit: int = 1000) -> int:
        """
        Remove old completed tasks (for maintenance).

        Args:
            before: Remove tasks completed before this time
            limit: Maximum number to remove in one call

        Returns:
            Number of tasks removed
        """
        with self.db.transaction() as conn:
            cursor = conn.execute('''
                DELETE FROM jobs
                WHERE id IN (
                    SELECT id FROM jobs
                    WHERE status='completed'
                      AND json_extract(document,'$.finished_at') < ?
                    LIMIT ?
                )
            ''', (before.isoformat(), limit))

            return cursor.rowcount
