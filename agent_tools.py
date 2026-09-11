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
            '用实际场景扫码记录确认相机进入场景；换场景结束旧仪器绑定，同场景重复确认幂等。'),
        'bind_instrument': (Bind, lambda a: core['bind'](a),
            '将实际扫码匹配的仪器绑定到该照片相机和实验员；仪器须已登记场景。使用用户指定的仪器和操作人，不猜测身份。会结束该相机之前的绑定。'),
        'read_panel': (Ocr, lambda a: core['submit_ocr'](a),
            '基于有效 binding_id 和 capture_id 异步提交真实 PaddleOCR。crop 为原图像素 [x,y,width,height]，省略则整图。返回 job_id；提交成功不等于识别完成。'),
        'get_panel_result': (JobId, lambda a: core['get_job'](a.job_id),
            '查询 OCR 任务。仅 completed 时读取 lines、confidence、polygon 和来源；queued/running 时稍后查询，failed/interrupted 时报告失败，不编造读数。'),
        'end_instrument_binding': (BindingId, lambda a: core['end_binding'](a.binding_id),
            '结束指定仪器使用绑定；重复结束保持同一结果。'),
    }

    @app.get('/api/tools')
    def list_tools():
        return {'version': '1.0', 'transport': 'http-json', 'tools': [
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
