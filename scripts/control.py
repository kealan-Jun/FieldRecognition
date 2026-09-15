"""Local start, stop, status and browser entry point; no model or NAS reads."""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

UNIT = 'field-recognition.target' if (Path.home()/'.config/systemd/user/field-recognition.target').exists() else 'field-recognition-demo.service'
URL = 'http://127.0.0.1:8188'


def status():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(URL + ('/health/live' if UNIT.endswith('.target') else '/api/state'), timeout=3) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description='现场识别：本机运行控制')
    parser.add_argument('action', choices=('open', 'start', 'stop', 'status'), nargs='?', default='open')
    action = parser.parse_args().action
    if action == 'stop':
        subprocess.run(['systemctl', '--user', 'stop', UNIT], check=True)
        print('本机识别服务已停止；再次开机仍会按自启配置启动。')
        return
    if action in ('start', 'open'):
        subprocess.run(['systemctl', '--user', 'start', UNIT], check=True)
    deadline = time.monotonic() + (15 if action in ('start', 'open') else 0)
    while True:
        try:
            data = status()
            break
        except (OSError, urllib.error.URLError, ValueError):
            if time.monotonic() >= deadline:
                print('识别服务暂不可用。请运行 systemctl --user status ' + UNIT, file=sys.stderr)
                sys.exit(1)
            time.sleep(.5)
    if UNIT.endswith('.target'):
        print('本机托管服务已启动，打开工作台查看相机、读数与归档状态。')
        print('页面：'+URL)
        if action=='open':subprocess.run(['xdg-open',URL],check=True)
        return
    automatic = data.get('automation', {})
    print('现场识别：' + automatic.get('message', '服务已启动'))
    print('实验员登记：' + (automatic.get('operator') or '等待使用者填写'))
    print('相机：' + data.get('camera', {}).get('status', '未配置'))
    print('OCR：' + data.get('ocr', {}).get('device', '未知') + ' · ' + data.get('ocr', {}).get('status', '未知'))
    print('照片监控：' + data.get('photo_watch', {}).get('status', '未知'))
    archive = data.get('archive', {})
    print('NAS 留存：' + {'disabled': '未启用', 'ready': '已连接', 'retrying': '等待重试'}.get(archive.get('status'), '正在检查')
          + f" · 已归档 {archive.get('archived_receipts', 0)} 条，待同步 {archive.get('pending_receipts', 0)} 条")
    print('页面：' + URL)
    if action == 'open':
        subprocess.run(['xdg-open', URL], check=True)


if __name__ == '__main__':
    main()
