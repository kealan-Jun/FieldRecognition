"""Framework-neutral JSON tools; every operation uses the existing application core."""
import base64
import binascii
import uuid

from fastapi import Body, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Photo(Arguments):
    camera_id: str = Field(min_length=1, max_length=100)
    image_base64: str = Field(min_length=1, max_length=16 * 1024 * 1024)


class BindingId(Arguments):
    binding_id: uuid.UUID


class JobId(Arguments):
    job_id: uuid.UUID


def install_tools(app, core):
    # Reuse original request contracts, while rejecting misspelled tool arguments.
    class Bind(core['BindingRequest']):
        model_config = ConfigDict(extra='forbid')

    class EnterScene(core['SceneEntry']):
        model_config = ConfigDict(extra='forbid')

    class Ocr(core['OcrRequest']):
        model_config = ConfigDict(extra='forbid')

    def scan_photo(args):
        if not args.camera_id.strip():
            raise HTTPException(422, 'camera_id must not be blank')
        try:
            raw = base64.b64decode(args.image_base64, validate=True)
        except (ValueError, binascii.Error):
            raise HTTPException(422, 'image_base64 must contain plain base64 image bytes') from None
        return core['scan_image'](raw, 'uploaded_photo', args.camera_id)

    registry = {
        'get_field_state': (Arguments, lambda a: core['state'](),
            '查询已登记仪器、场景、当前绑定及相机/OCR状态。'),
        'capture_and_scan': (Arguments, lambda a: core['camera_capture'](),
            '从服务配置的挂脖相机取得最新照片并识别场景和仪器二维码；返回 capture_id、scan_id、匹配仪器及帧来源。无二维码也保留照片，可用于已有绑定的面板 OCR。'),
        'scan_photo': (Photo, scan_photo,
            '识别调用方提供的照片中的场景和仪器二维码并保存原图证据。传图片的纯 base64 和来源 camera_id，不传路径或 URL。'),
        'enter_scene': (EnterScene, lambda a: core['enter_scene'](a),
            '用实际场景扫码记录确认相机进入场景；已有仪器绑定时拒绝换场景，同场景重复确认幂等。'),
        'bind_instrument': (Bind, lambda a: core['bind'](a),
            '将实际扫码匹配的仪器绑定到该照片相机和实验员；仪器须已登记场景。使用用户指定的仪器和操作人，不猜测身份。同相机、仪器与实验员重复请求返回原绑定；已有其他有效绑定时拒绝覆盖。'),
        'read_panel': (Ocr, lambda a: core['submit_ocr'](a),
            '仅在用户要求查看面板读数时，基于有效 binding_id 和已有 capture_id 提交识别。先用常驻 PaddleOCR；该次照片请求开始后 5 秒仍无数字时使用视觉兜底。crop 为原图像素 [x,y,width,height]，省略则整图。返回 job_id，需查询结果；不要因绑定成功自动调用。'),
        'read_saved_panel': (core['SavedPhotoRequest'], lambda a: core['read_saved_panel'](a),
            '用户要求查看面板读数、现有 Agent 拍照工具已把照片保存到 NAS 后调用。传当前 binding_id 和拍照结果指定的 image_path（语音拍照目录内的具体 JPG/PNG 路径，或相对于 voice_photos 的路径）；也可传 photo 原图 base64 与拍照回执，两者只能选一个。只读取指定照片；目录监控由本服务根据有效绑定自动完成。先运行常驻 OCR；本次请求 5 秒无数字时最多一次视觉兜底。同一照片和选框重复提交返回原 job_id。随后 get_panel_result 查询并统一回答读数，无需单独标注云端来源。'),
        'get_panel_result': (JobId, lambda a: core['get_job'](a.job_id),
            '查询识别任务。completed 时统一读取 lines；confidence、polygon 可能为 null，不能补造。queued/running 时稍后查询原 job_id；failed/interrupted/cancelled 时报告未完成，不重新提交、不编造读数。后台 local_ocr/fallback 保留审计来源，用户读数无需另作云端标注。'),
        'end_instrument_binding': (BindingId, lambda a: core['end_binding'](a.binding_id),
            '结束指定仪器使用绑定；重复结束保持同一结果。'),
    }

    @app.get('/api/tools')
    def list_tools():
        return {'version': '1.1', 'transport': 'http-json', 'tools': [
            {'name': name, 'description': desc, 'input_schema': model.model_json_schema()}
            for name, (model, _, desc) in registry.items()]}

    @app.post('/api/tools/{tool_name}')
    def call_tool(tool_name: str, arguments: dict = Body(...)):
        if tool_name not in registry:
            raise HTTPException(404, 'Unknown tool')
        model, handler, _ = registry[tool_name]
        try:
            parsed = model.model_validate(arguments)
        except ValidationError as exc:
            # Do not echo image bytes or argument values in validation errors.
            raise HTTPException(422, exc.errors(include_input=False, include_context=False)) from None
        result = handler(parsed)
        # Internal storage paths are not portable Agent image references.
        if isinstance(result, dict):
            result = {k: v for k, v in result.items() if k != 'path'}
        return {'tool': tool_name, 'result': result}
