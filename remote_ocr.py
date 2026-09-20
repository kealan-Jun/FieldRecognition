"""Local business pipeline client for the separately scheduled GPU service."""
import contextvars
import hashlib
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import cv2
import httpx


class InferenceUnavailable(RuntimeError):
    """A dependency outage; keep the business task queued without exhausting retries."""


class InferenceRejected(RuntimeError):
    pass


request_id = contextvars.ContextVar('ocr_request_id', default=None)


@contextmanager
def inference_request(identity=None):
    token = request_id.set(identity or request_id.get() or uuid.uuid4().hex)
    try:
        yield
    finally:
        request_id.reset(token)


class RemoteOcr:
    def __init__(self):
        self.url = os.environ['FIELD_OCR_SERVICE_URL'].rstrip('/')
        parsed = urlparse(self.url)
        if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}):
            raise ValueError('OCR requires HTTPS or a loopback endpoint')
        self.token_file = Path(os.environ['FIELD_OCR_SERVICE_TOKEN_FILE'])
        self.scope = os.environ['FIELD_OCR_SERVICE_SCOPE']
        self.storage = os.environ['FIELD_OCR_SERVICE_STORAGE']
        self.revision = os.environ['FIELD_OCR_SERVICE_MODEL_REVISION']
        self.spool = Path(os.environ['FIELD_OCR_SERVICE_SPOOL'])
        if not self.spool.is_dir():
            raise ValueError('OCR spool directory must be provisioned first')
        self.client = httpx.Client(timeout=3, trust_env=False, follow_redirects=False,
                                   verify=os.environ.get('FIELD_OCR_SERVICE_CA') or True)
        self.last_receipt = None
        self.cleanup_at = 0

    def _request(self, method, path, *, key=None, body=None):
        try:
            headers = {'Authorization': 'Bearer ' + self.token_file.read_text().strip(),
                       'X-Execution-Scope': self.scope}
            if key:
                headers['Idempotency-Key'] = key
            response = self.client.request(method, self.url + path, headers=headers, json=body)
        except (httpx.HTTPError, OSError) as exc:
            raise InferenceUnavailable('推理服务暂不可用，任务等待恢复') from exc
        if response.status_code in {401, 403, 408, 429} or response.status_code >= 500:
            raise InferenceUnavailable('推理服务或接入配置暂不可用，任务等待恢复')
        if response.status_code >= 400:
            raise InferenceRejected('推理请求被拒绝：HTTP ' + str(response.status_code))
        try:
            return response.json()
        except ValueError as exc:
            raise InferenceUnavailable('推理服务返回异常，任务等待恢复') from exc

    def health(self):
        try:
            health = self._request('GET', '/health/ready')
            workers = health.get('workers', {})
            ready = bool(workers.get('ready') or workers.get('running'))
            return {'status': 'ready' if ready else 'waiting_service', 'resident': ready,
                    'backend': 'standalone_gpu_service', 'error': None,
                    'error_detail': None if ready else '等待 GPU 推理时段或模型恢复，照片任务保留在队列'}
        except InferenceUnavailable:
            return {'status': 'waiting_service', 'resident': False,
                    'backend': 'standalone_gpu_service', 'error': 'InferenceUnavailable',
                    'error_detail': '推理服务暂不可用，照片任务保留在队列'}

    def cleanup(self):
        """Remove only transport files whose durable remote jobs have finished."""
        if time.monotonic() < self.cleanup_at:
            return
        self.cleanup_at = time.monotonic()+60
        for path in list(self.spool.glob('*.png'))[:20]:
            try:
                age = time.time()-path.stat().st_mtime
            except FileNotFoundError:
                continue
            if age < 60:
                continue
            try:
                job = self._request('GET', '/v1/ocr/submissions/' + path.stem)
                if job['state'] in {'succeeded', 'failed', 'cancelled'}:
                    path.unlink(missing_ok=True)
            except (InferenceUnavailable, InferenceRejected):
                break

    def predict(self, image):
        self.last_receipt = None
        # Do not accumulate live frame files during a scheduled GPU pause/outage.
        if not self.health()['resident']:
            raise InferenceUnavailable('等待 GPU 推理服务恢复')
        ok, png = cv2.imencode('.png', image)
        if not ok:
            raise ValueError('Cannot encode OCR region')
        raw = png.tobytes()
        digest = hashlib.sha256(raw).hexdigest()
        identity = request_id.get() or uuid.uuid4().hex
        key = hashlib.sha256((identity + ':' + digest + ':' + self.revision).encode()).hexdigest()
        path = self.spool / (key + '.png')
        # Repeat the same immutable input and idempotency key after process/network failure.
        with tempfile.NamedTemporaryFile(dir=self.spool, prefix='.Writing-', delete=False) as handle:
            temporary = Path(handle.name)
            try:
                os.fchmod(handle.fileno(), 0o640)
                handle.write(raw); handle.flush(); os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        body = {'asset': {'asset_id': key, 'source_revision': digest, 'storage_id': self.storage,
                          'object_key': path.name, 'sha256': digest}, 'correlation_id': identity}
        job = self._request('POST', '/v1/ocr/jobs', key=key, body=body)
        deadline = time.monotonic() + 12
        while job['state'] not in {'succeeded', 'failed', 'cancelled'}:
            if time.monotonic() >= deadline:
                raise InferenceUnavailable('GPU 任务仍在排队，稍后按原任务继续')
            time.sleep(.15)
            job = self._request('GET', '/v1/ocr/jobs/' + job['job_id'])
        if job['state'] != 'succeeded':
            path.unlink(missing_ok=True)
            raise InferenceRejected('GPU 任务未成功，保留原任务回执')
        result = self._request('GET', '/v1/ocr/jobs/' + job['job_id'] + '/result')
        if (result['source']['sha256'] != digest or result['model']['weights_revision'] != self.revision
                or not result['execution']['actual_model_invocation']):
            raise InferenceRejected('推理结果的图像、模型或执行凭证不匹配')
        self.last_receipt = result
        # This is a transport crop, never an archive photo. The original business image remains.
        path.unlink(missing_ok=True)
        lines = result['lines']
        return [{'rec_texts': [v['text'] for v in lines], 'rec_scores': [v['confidence'] for v in lines],
                 'rec_polys': [v['polygon'] for v in lines]}]
