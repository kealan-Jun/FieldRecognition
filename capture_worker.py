"""One isolated capture runtime per registered camera, with shared GPU inference IPC."""
import argparse
import asyncio
import os
import signal
import uuid

from runtime_rpc import RpcServer
from worker_support import process_lock, heartbeat


async def run():
    import app
    from automation import AutomationSettings
    from video_ocr import VideoSettings
    from live_scan import preview_part, recognition_part
    core=vars(app)
    camera=app.receiver_camera
    target=os.environ['FIELD_CAMERA_ID']
    def preview(recognition,previous):
        # JSON transport changes tuple tokens to lists; restore their shape so
        # the same frame is not encoded and delivered repeatedly.
        def token(value):return tuple(token(v) for v in value) if isinstance(value,list) else value
        previous=token(previous)
        return recognition_part(app.video_ocr,camera,previous) if recognition else preview_part(camera,previous)
    with process_lock(app.DATA,'capture-'+target):
        handlers={
            'receiver.snapshot':camera.snapshot,
            'receiver.frame':camera.frame,
            'receiver.preview':preview,
            'scanner.snapshot':lambda:app.live_scanner.session,
            'scanner.active_bindings':app.live_scanner.active_bindings,
            'scanner.active_binding':app.live_scanner.active_binding,
            'scanner.start':app.live_scanner.start,
            'scanner.refresh_bindings':app.live_scanner.refresh_bindings,
            'scanner.read':lambda sid,**kw:app.live_scanner.read(uuid.UUID(sid),**kw),
            'scanner.observe_service':app.live_scanner.observe_service,
            'automation.snapshot':app.automatic_runner.snapshot,
            'automation.settings':app.automatic_runner.settings,
            'automation.configure':lambda body:app.automatic_runner.configure(AutomationSettings(**body)),
            'automation.pause':app.automatic_runner.pause,
            'automation._save':app.automatic_runner._save,
            'video.snapshot':app.video_ocr.snapshot,
            'video.configure':lambda body:app.video_ocr.configure(VideoSettings(**body)),
            'video.preview':app.video_ocr.preview,
            'watcher.snapshot':app.saved_photo_watcher.snapshot,
            'watcher.enabled':app.saved_photo_watcher.enabled,
        }
        server=RpcServer('camera:'+target,handlers);server.start()
        stop=asyncio.Event()
        loop=asyncio.get_running_loop()
        for sig in (signal.SIGINT,signal.SIGTERM):loop.add_signal_handler(sig,stop.set)
        try:
            async with app.lifespan(app.app):
                while not stop.is_set():
                    # This status is local only; slow NAS polling occurs on its own thread.
                    heartbeat(app.database,'camera:'+target,{'status':'running','camera_id':target,'receiver':camera.snapshot()})
                    try:await asyncio.wait_for(stop.wait(),timeout=2)
                    except asyncio.TimeoutError:pass
        finally:
            server.close();heartbeat(app.database,'camera:'+target,{'status':'stopped'})


if __name__=='__main__':
    os.environ['FIELD_SERVICE_ROLE']='capture'
    asyncio.run(run())
