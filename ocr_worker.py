"""Process isolation: Separate OCR worker process."""
import argparse
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from database import Database
from task_queue import TaskQueue
from production_config import ProductionConfig


class OCRWorker:
    """Dedicated OCR worker process."""

    def __init__(self, worker_id: str, config: ProductionConfig):
        self.worker_id = worker_id
        self.config = config
        self.db = Database(config.database_path)
        self.queue = TaskQueue(self.db, worker_id)
        self.running = True
        self.ocr_model = None

        # Setup signal handlers
        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        """Handle graceful shutdown."""
        print(f"[{self.worker_id}] Received shutdown signal, finishing current task...")
        self.running = False

    def _load_ocr_model(self):
        """Load OCR model once."""
        if self.ocr_model is not None:
            return

        print(f"[{self.worker_id}] Loading OCR model...")
        from ocr_runtime import create_model

        try:
            self.ocr_model = create_model(self.config.ocr_device)
            print(f"[{self.worker_id}] OCR model loaded successfully")
        except Exception as e:
            print(f"[{self.worker_id}] Failed to load OCR model: {e}")
            raise

    def _process_ocr_task(self, task: dict):
        """Process a single OCR task."""
        job_id = task['job_id']
        print(f"[{self.worker_id}] Processing OCR job {job_id}")

        try:
            # Ensure model is loaded
            self._load_ocr_model()

            # Renew lease for long tasks
            self.queue.renew_lease(job_id)

            # Import OCR processing logic
            from panel_readout import process_panel_job

            # Process the job
            result = process_panel_job(task, self.ocr_model)

            # Complete the task
            self.queue.complete_task(job_id, result)
            print(f"[{self.worker_id}] Completed job {job_id}")

        except Exception as e:
            print(f"[{self.worker_id}] Job {job_id} failed: {e}")
            self.queue.fail_task(job_id, str(e), retry=True)

    def run(self):
        """Main worker loop."""
        print(f"[{self.worker_id}] OCR worker started")
        print(f"[{self.worker_id}] Device: {self.config.ocr_device}")

        idle_count = 0
        max_idle = 30  # Exit after 30 idle loops (30 seconds)

        while self.running:
            # Claim a task
            task = self.queue.claim_task(task_type='ocr')

            if task:
                idle_count = 0
                self._process_ocr_task(task)
            else:
                idle_count += 1
                if idle_count >= max_idle:
                    print(f"[{self.worker_id}] No tasks for {max_idle}s, exiting")
                    break

                time.sleep(1)  # Wait before checking again

        print(f"[{self.worker_id}] Worker shutting down")
        self.db.close()


def main():
    """OCR worker entry point."""
    parser = argparse.ArgumentParser(description='FieldRecognition OCR Worker')
    parser.add_argument('--worker-id', default=None, help='Worker identifier')
    parser.add_argument('--persistent', action='store_true', help='Keep running even when idle')
    args = parser.parse_args()

    # Load configuration
    config = ProductionConfig.from_environment()

    # Generate worker ID if not provided
    worker_id = args.worker_id or f'ocr-worker-{os.getpid()}'

    # Create and run worker
    worker = OCRWorker(worker_id, config)

    if args.persistent:
        # Override idle timeout for persistent workers
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
