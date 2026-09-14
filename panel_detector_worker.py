"""Private stdin/stdout inference protocol; no listening port or frame retention."""
import base64
import contextlib
import json
import sys


def main():
    wire = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        import cv2
        import numpy as np
        import torch
        from ultralytics import YOLO, settings
        settings.update({'sync': False})
        torch.set_num_threads(2)
        if not torch.cuda.is_available():
            raise RuntimeError('GPU unavailable')
        model = YOLO(sys.argv[1])
        imgsz, confidence = int(sys.argv[2]), float(sys.argv[3])
        model.predict(np.zeros((800, 1280, 3), np.uint8), device=0, imgsz=imgsz, verbose=False)
    wire.write(json.dumps({'status':'ready', 'names':model.names})+'\n'); wire.flush()
    for request in sys.stdin:
        try:
            raw = base64.b64decode(json.loads(request)['image'], validate=True)
            image = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
            if image is None:
                raise ValueError('invalid image')
            with contextlib.redirect_stdout(sys.stderr):
                result = model.predict(image, device=0, imgsz=imgsz, conf=confidence, max_det=6, verbose=False)[0]
            response = {'boxes': [{'class_id':int(c), 'confidence':float(s), 'xyxy':b}
                for b,c,s in zip(result.boxes.xyxy.cpu().tolist(), result.boxes.cls.cpu().tolist(), result.boxes.conf.cpu().tolist())],
                'speed_ms':result.speed}
        except Exception as exc:
            response = {'error':type(exc).__name__}
        wire.write(json.dumps(response)+'\n'); wire.flush()


if __name__ == '__main__':
    main()
