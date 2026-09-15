"""Archive worker: Separate process for NAS archiving."""
import argparse
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from database import Database
from production_config import ProductionConfig


class ArchiveWorker:
    """Dedicated archive worker process."""

    def __init__(self, worker_id: str, config: ProductionConfig):
        self.worker_id = worker_id
        self.config = config
        self.db = Database(config.database_path)
        self.running = True
        self.archive_store = None

        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        """Handle graceful shutdown."""
        print(f"[{self.worker_id}] Received shutdown signal, finishing current batch...")
        self.running = False

    def _init_archive_store(self):
        """Initialize archive store."""
        if self.archive_store is not None:
            return

        if not self.config.archive_enabled:
            raise RuntimeError("Archive not enabled in configuration")

        print(f"[{self.worker_id}] Initializing archive store...")
        from archive_store import ArchiveStore

        # Create core dict for ArchiveStore
        core = {
            'db': lambda: self.db.connection(),
            'now': lambda: __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
            'DATA': Path(self.config.database_path).parent
        }

        self.archive_store = ArchiveStore(core)
        print(f"[{self.worker_id}] Archive store initialized")

    def _process_batch(self) -> int:
        """Process a batch of archive tasks."""
        if self.archive_store is None:
            self._init_archive_store()

        # Process pending items
        processed = 0

        with self.db.connection() as conn:
            rows = conn.execute('''
                SELECT entity, entity_id FROM archive_outbox
                WHERE archived_at IS NULL
                ORDER BY seq
                LIMIT 10
            ''').fetchall()

            for row in rows:
                if not self.running:
                    break

                try:
                    # Archive the item (this updates the database)
                    self.archive_store._archive_entity(row['entity'], row['entity_id'])
                    processed += 1
                except Exception as e:
                    print(f"[{self.worker_id}] Failed to archive {row['entity']}/{row['entity_id']}: {e}")

        return processed

    def run(self):
        """Main worker loop."""
        print(f"[{self.worker_id}] Archive worker started")
        print(f"[{self.worker_id}] Archive root: {self.config.archive_root}")

        idle_count = 0
        max_idle = 60  # Exit after 60 idle loops (60 seconds)

        while self.running:
            try:
                processed = self._process_batch()

                if processed > 0:
                    idle_count = 0
                    print(f"[{self.worker_id}] Archived {processed} items")
                else:
                    idle_count += 1
                    if idle_count >= max_idle:
                        print(f"[{self.worker_id}] No tasks for {max_idle}s, exiting")
                        break

                time.sleep(1)  # Throttle

            except Exception as e:
                print(f"[{self.worker_id}] Error in batch processing: {e}")
                time.sleep(5)  # Back off on errors

        print(f"[{self.worker_id}] Worker shutting down")
        self.db.close()


def main():
    """Archive worker entry point."""
    parser = argparse.ArgumentParser(description='FieldRecognition Archive Worker')
    parser.add_argument('--worker-id', default=None, help='Worker identifier')
    parser.add_argument('--persistent', action='store_true', help='Keep running even when idle')
    args = parser.parse_args()

    # Load configuration
    config = ProductionConfig.from_environment()

    if not config.archive_enabled:
        print("Archive not enabled in configuration", file=sys.stderr)
        return 1

    # Generate worker ID if not provided
    worker_id = args.worker_id or f'archive-worker-{os.getpid()}'

    # Create and run worker
    worker = ArchiveWorker(worker_id, config)

    if args.persistent:
        worker.max_idle = float('inf')

    try:
        worker.run()
        return 0
    except Exception as e:
        print(f"Worker crashed: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
