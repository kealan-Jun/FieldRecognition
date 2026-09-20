"""Fail closed when the managed business worker is configured to use an external GPU."""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--require-remote', action='store_true')
    args = parser.parse_args()
    if args.require_remote and not os.environ.get('FIELD_OCR_SERVICE_URL'):
        raise SystemExit('Remote OCR configuration required; refusing to load another GPU model')
    if os.environ.get('FIELD_OCR_SERVICE_URL'):
        if os.environ.get('FIELD_PANEL_DEVICE') != 'cpu' or os.environ.get('CUDA_VISIBLE_DEVICES') != '':
            raise SystemExit('Remote OCR business worker must not own CUDA')
        from remote_ocr import RemoteOcr
        model = RemoteOcr()
        if not model.token_file.is_file():
            raise SystemExit('Remote OCR credential file unavailable')
        model.client.close()


if __name__ == '__main__':
    main()
