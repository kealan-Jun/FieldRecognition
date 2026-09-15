"""Resident detector client; PyTorch lives in its own project environment/process."""
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import subprocess
import threading
import time

import cv2


class PanelDetector:
    def __init__(self):
        self.process = None
        self.lock = threading.Lock()
        self.config = None
        self.state = {'status': 'not_loaded', 'resident': False, 'model': 'YOLO11n', 'load_count': 0}

    def enabled(self):
        return os.environ.get('FIELD_PANEL_DETECTOR_ENABLED', '0').lower() in {'1', 'true', 'yes'}

    def snapshot(self):
        return dict(self.state, enabled=self.enabled())

    def close(self):
        process, self.process = self.process, None
        if process:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            for pipe in (process.stdin, process.stdout):
                if pipe:
                    pipe.close()
        self.state.update(resident=False)

    def _receive(self, timeout):
        with selectors.DefaultSelector() as poll:
            poll.register(self.process.stdout, selectors.EVENT_READ)
            if not poll.select(timeout):
                raise TimeoutError('panel detector timeout')
            raw = self.process.stdout.readline(1024 * 1024)
        if not raw or not raw.endswith(b'\n'):
            raise RuntimeError('panel detector protocol interrupted')
        result = json.loads(raw)
        if result.get('error'):
            raise RuntimeError(result['error'])
        return result

    def _start(self):
        if self.process and self.process.poll() is None:
            return
        self.close()
        self.state.update(status='loading', error=None)
        path = Path(os.environ['FIELD_PANEL_MODEL_MANIFEST'])
        config = json.loads(path.read_text())
        weights = Path(config['weights'])
        digest = hashlib.sha256(weights.read_bytes()).hexdigest()
        from panel_layout import normalize_classes
        config['classes'] = normalize_classes(config['classes'])
        if digest != config['weights_sha256']:
            raise ValueError('panel model manifest mismatch')
        self.config = config
        env = {k: os.environ[k] for k in ('PATH', 'HOME', 'LANG') if k in os.environ}
        env.update(OMP_NUM_THREADS='2', OPENBLAS_NUM_THREADS='2', YOLO_AUTOINSTALL='false',
                   YOLO_OFFLINE='true', YOLO_CONFIG_DIR=str(path.parent / 'runtime'))
        self.process = subprocess.Popen([config['python'], '-u', str(Path(__file__).with_name('panel_detector_worker.py')),
            str(weights), str(config.get('imgsz', 960)), str(config.get('confidence', .25))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=None, env=env)
        ready = self._receive(60)
        if ready.get('status') != 'ready' or ready.get('names') != {key:value['name'] for key,value in config['classes'].items()}:
            raise ValueError('unexpected detector classes')
        self.state.update(status='ready', resident=True, error=None, device='gpu:0',
            weights_sha256=digest, model_version=config['version'],
            load_count=self.state['load_count'] + 1, confidence_threshold=config.get('confidence', .25))

    def warmup(self):
        if not self.enabled():
            return False
        with self.lock:
            try:
                self._start()
                return True
            except Exception as exc:
                self.close()
                self.state.update(status='error', error=type(exc).__name__)
                return False

    def predict(self, image):
        with self.lock:
            started = time.monotonic()
            try:
                self._start()
                ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 95])
                if not ok:
                    raise ValueError('detector input encoding failed')
                self.process.stdin.write(json.dumps({'image': base64.b64encode(encoded).decode()}).encode() + b'\n')
                self.process.stdin.flush()
                result = self._receive(10)
                boxes = []
                h, w = image.shape[:2]
                for item in result['boxes'][:6]:
                    key = str(item['class_id'])
                    values = item['xyxy'] + [item['confidence']]
                    if key not in self.config['classes'] or not all(math.isfinite(v) for v in values):
                        raise ValueError('invalid detector output')
                    x1, y1, x2, y2 = item['xyxy']
                    x1, y1 = max(0, math.floor(x1)), max(0, math.floor(y1))
                    x2, y2 = min(w, math.ceil(x2)), min(h, math.ceil(y2))
                    if x2-x1 < 4 or y2-y1 < 4:
                        continue
                    boxes.append(item | {'xyxy': [x1,y1,x2,y2],
                        **{field:self.config['classes'][key].get(field) for field in ('instrument_id','type_id','layout','measurements')}})
                boxes.sort(key=lambda b: (b['class_id'], b['xyxy'][0], b['xyxy'][1]))
                self.state.update(status='ready', error=None, last_seconds=round(time.monotonic()-started, 3),
                                  recovery=result.get('recovery'))
                return {'status': 'completed', 'boxes': boxes, 'model': 'YOLO11n',
                    'weights_sha256': self.state['weights_sha256'], 'model_version': self.config['version'],
                    'device': 'gpu:0', 'wall_seconds': self.state['last_seconds'], 'speed_ms': result.get('speed_ms'),
                    'recovery':result.get('recovery')}
            except Exception as exc:
                self.close()
                self.state.update(status='error', error=type(exc).__name__)
                return {'status': 'failed', 'boxes': [], 'error': type(exc).__name__, 'model': 'YOLO11n'}
