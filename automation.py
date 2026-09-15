"""Persistent, browser-independent scanning for the configured local camera."""
import copy
import json
import os
import threading
import time

from fastapi import HTTPException
from pydantic import BaseModel, Field

from live_scan import ACTIVE


class AutomationSettings(BaseModel):
    enabled: bool
    operator: str | None = Field(default=None, max_length=80)


class AutomaticRunner:
    def __init__(self, core):
        self.core = core
        self.scanner = core['live_scanner']
        self.lock = self.scanner.lock
        self.stop = threading.Event()
        self.worker = None
        self.retry_at = 0
        self.failed_session = None
        self.state = {'status': 'starting', 'message': '正在检查自动运行配置'}
        with core['db']() as conn:
            conn.execute('CREATE TABLE IF NOT EXISTS automation_settings(camera TEXT PRIMARY KEY, document TEXT NOT NULL)')

    def target(self):
        camera = self.core['receiver_camera']
        return camera.target if camera else os.environ.get('FIELD_CAMERA_ID', 'UnconfiguredNeckCamera')

    def settings(self):
        with self.core['db']() as conn:
            row = conn.execute('SELECT document FROM automation_settings WHERE camera=?', (self.target(),)).fetchone()
        if row:
            settings = json.loads(row['document'])
            if self.core.get('RUNTIME_ENABLED'):
                with self.core['db']() as conn:
                    owner=conn.execute('SELECT u.id,u.display_name FROM camera_users c JOIN users u ON u.id=c.user_id WHERE c.camera_id=? AND u.disabled=0',(self.target(),)).fetchone()
                if not owner:return settings | {'enabled':False,'operator':'','pause_reason':'unassigned_camera'}
                settings.update(operator=owner['display_name'],wearer_id=owner['id'])
            return settings
        return {'enabled': os.environ.get('FIELD_AUTO_RUN_ENABLED', '0').lower() in {'1', 'true', 'yes'},
                'operator': '', 'registered_at': None, 'pause_reason': None}

    def _save(self, settings):
        with self.core['db']() as conn:
            conn.execute('INSERT OR REPLACE INTO automation_settings VALUES(?,?)',
                         (self.target(), json.dumps(settings)))

    def configure(self, body):
        with self.lock:
            settings = self.settings()
            operator = settings['operator'] if body.operator is None else body.operator.strip()
            if body.enabled and not operator:
                raise HTTPException(422, '请先登记实验员姓名或编号，再开启自动运行')
            binding = self.scanner.active_binding() if self.core['receiver_camera'] else None
            if binding and (body.enabled or body.operator is not None) and operator != binding['operator']:
                raise HTTPException(409, '请先结束当前仪器绑定，再登记新的实验员；历史记录会保留')
            for visit in self.core['scene_visits']():
                if (visit['camera_id'] == self.target() and visit.get('operator')
                        and (body.enabled or body.operator is not None) and operator != visit['operator']):
                    raise HTTPException(409, '请先结束当前场景关联，再登记新的实验员；历史记录会保留')
            self._stop_scan('自动运行设置已更新')
            settings.update(enabled=body.enabled, operator=operator,
                            pause_reason=None if body.enabled else 'user_paused')
            if body.operator is not None:
                settings['registered_at'] = self.core['now']()
            self._save(settings)
            self.failed_session, self.retry_at = None, 0
            # A reviewed resume starts a fresh scan, including after a multi-code pause.
            if self.scanner.session and self.scanner.session.get('owner') == 'automation':
                self.scanner.session = None
            self.step()
            return self.snapshot()

    def pause(self, reason='user_paused'):
        with self.lock:
            settings = self.settings()
            settings.update(enabled=False, pause_reason=reason)
            self._save(settings)
            self._stop_scan('自动扫码已暂停；已有绑定保留')
            self.state = {'status': 'paused', 'message': {
                'binding_ended': '本次绑定已结束，自动扫码已暂停；下次使用时点击保存并开启',
                'needs_selection': '二维码需要人工选择，自动扫码已暂停；请核对画面或重新开始',
            }.get(reason, '自动扫码已暂停；已有绑定和照片监控保持原有状态')}

    def _stop_scan(self, message):
        session = self.scanner.session
        if session and session.get('owner') == 'automation' and session['status'] in ACTIVE:
            session.update(status='stopped', message=message)

    def snapshot(self):
        with self.lock:
            settings = self.settings()
            session = self.scanner.session
            return copy.deepcopy(settings | self.state | {
                'binding_ids': [b['binding_id'] for b in self.scanner.active_bindings()] if self.core['receiver_camera'] else [],
                'camera_id': self.target(), 'browser_required': False,
                'session': session if session and session.get('owner') == 'automation' else None})

    def step(self):
        with self.lock:
            if self.stop.is_set():
                return
            settings = self.settings()
            if not settings['enabled']:
                self._stop_scan('自动扫码未开启')
                if self.state['status'] != 'paused':
                    self.state = {'status': 'paused', 'message': '自动扫码已暂停，登记实验员后可开启'}
                return
            if not settings['operator']:
                self._stop_scan('等待实验员登记')
                self.state = {'status': 'waiting_operator', 'message': '请先登记实验员姓名或编号，保存后自动扫码'}
                return
            camera = self.core['receiver_camera']
            if camera is None:
                self.state = {'status': 'waiting_config', 'message': '等待配置挂脖相机接收端'}
                return
            info = camera.snapshot()
            # Never authorize unattended binding from a persisted or unavailable status.
            if not info.get('service_status_available') or not info.get('service_status', {}).get('online'):
                self._stop_scan('等待相机采集服务状态恢复')
                self.state = {'status': 'waiting_camera', 'message': '等待相机采集服务上线，恢复后自动扫码'}
                return
            session = self.scanner.session
            if session and session.get('owner') != 'automation' and session['status'] in ACTIVE:
                self.state = {'status': 'manual_scan', 'message': '当前由手动扫码会话控制'}
                return
            if session and session.get('owner') == 'automation':
                if session['status'] == 'needs_selection':
                    self.pause('needs_selection')
                    return
                if session['status'] == 'failed' and self.failed_session != session['session_id']:
                    self.failed_session = session['session_id']
                    self.retry_at = time.monotonic() + 30
                if time.monotonic() < self.retry_at:
                    self.state = {'status': 'retrying', 'message': '扫码暂不可用，30 秒后自动重试'}
                    return
                if session['status'] in ACTIVE:
                    self.scanner.refresh_bindings()
                    self.state = {'status': session['status'], 'message': session['message'],
                                  'binding_ids': [b['binding_id'] for b in session['bindings']]}
                    return
            session = self.scanner.start(settings['operator'], owner='automation')
            self.state = {'status': session['status'], 'message': session['message']}

    def start(self):
        if self.worker is not None:
            return
        self.worker = threading.Thread(target=self._run, name='automatic-field-runner', daemon=True)
        self.worker.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                self.step()
            except Exception as exc:
                with self.lock:
                    self._stop_scan('自动运行暂不可用，等待重试')
                    self.state = {'status': 'retrying', 'message': '自动运行暂不可用，正在等待重试',
                                  'error_type': type(exc).__name__}
                self.stop.wait(5)
            self.stop.wait(1)

    def close(self):
        self.stop.set()
        with self.lock:
            self._stop_scan('本地识别服务正在停止')
        if self.worker:
            self.worker.join(timeout=3)


def install(core):
    from runtime_rpc import camera_component
    runner = camera_component('automation', os.environ.get('FIELD_CAMERA_ID')) or AutomaticRunner(core)

    @core['app'].get('/api/automation')
    def state():
        return runner.snapshot()

    @core['app'].put('/api/automation')
    def configure(body: AutomationSettings):
        return runner.configure(body)

    return runner
