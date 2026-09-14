"""Bounded real GPU validation with a synthetic panel and temporary database."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('Verification/GpuOcr'))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    # Importing the application must never touch production records or external services.
    for key in ('FIELD_RECEIVER_URL', 'FIELD_CAMERA_SNAPSHOT_URL', 'DASHSCOPE_API_KEY'):
        os.environ.pop(key, None)
    os.environ.update(FIELD_OCR_DEVICE='gpu:0', FIELD_AUTO_RUN_ENABLED='0',
                      FIELD_SAVED_PHOTO_WATCH_ENABLED='0', FIELD_ALIYUN_FALLBACK_ENABLED='0',
                      FLAGS_allocator_strategy='auto_growth')
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    with tempfile.TemporaryDirectory(prefix='field-gpu-verify-') as data:
        os.environ['FIELD_DEMO_DATA'] = data
        import app
        import cv2
        import numpy as np

        panel = np.full((260, 720, 3), 255, dtype=np.uint8)
        cv2.putText(panel, '123.45', (70, 170), cv2.FONT_HERSHEY_SIMPLEX, 3.2, (0, 0, 0), 6, cv2.LINE_AA)
        cv2.imwrite(str(args.output / 'SyntheticPanel.png'), panel)
        try:
            started = time.monotonic()
            model = app.load_ocr()
            load_seconds = round(time.monotonic() - started, 3)
            results = [app.predict_panel(panel) for _ in range(2)]
            import paddle
            paddle.device.synchronize()
            processes = subprocess.check_output([
                'nvidia-smi', '--query-compute-apps=pid,gpu_uuid,used_memory',
                '--format=csv,noheader,nounits'], text=True).splitlines()
            own_gpu = [line for line in processes if line.split(',')[0].strip() == str(os.getpid())]
            passed = (paddle.is_compiled_with_cuda() and paddle.get_device() == 'gpu:0'
                      and bool(own_gpu) and app.load_ocr() is model and app.ocr_state['load_count'] == 1
                      and all(r['status'] == 'completed' and r['device'] == 'gpu:0'
                              and any('123.45' in line['numeric_candidates'] for line in r['lines'])
                              for r in results))
            receipt = {'status': 'PROVEN' if passed else 'NOT_PROVEN',
                       'scope': 'GPU inference and resident reuse on a synthetic panel only',
                       'physical_instrument_accuracy': 'NOT_PROVEN',
                       'paddle_version': paddle.__version__, 'cuda_version': paddle.version.cuda(),
                       'device': paddle.get_device(), 'gpu_name': paddle.device.cuda.get_device_name(0),
                       'process_gpu_memory': own_gpu, 'load_seconds': load_seconds,
                       'ocr_state': dict(app.ocr_state), 'results': results}
            (args.output / 'GpuReceipt.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
            print(json.dumps(receipt, ensure_ascii=False))
            if not passed:
                raise SystemExit('GPU validation did not satisfy every gate; inspect the receipt')
        finally:
            app.ocr_pool.shutdown(wait=True)
            app.readout_pool.shutdown(wait=True)


if __name__ == '__main__':
    main()
