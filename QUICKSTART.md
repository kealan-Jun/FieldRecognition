# 生产工程改造 - 快速部署指南

## 快速开始

### 1. 验证安装

```bash
cd /home/x1/Projects/FieldRecognition

# 运行集成测试
.venv/bin/python tests/test_integration.py

# 检查迁移状态
.venv/bin/python migrate_db.py --status
```

### 2. 启用生产功能（可选）

生产功能默认禁用，需要显式启用：

```bash
export FIELD_PRODUCTION_ENABLED=1
export FIELD_AUTH_ENABLED=1
export FIELD_RECORD_MODE=production

# 创建初始管理员
python production_bootstrap.py --create-admin
```

### 3. 测试模式运行（当前推荐）

```bash
# 使用现有方式启动（生产功能未集成到app.py）
unset FIELD_PRODUCTION_ENABLED
.venv/bin/python -m uvicorn app:app --host 127.0.0.1 --port 8188
```

## 当前状态

### ✅ 已完成的基础设施

1. **数据库迁移系统** - `database.py`, `migrate_db.py`
2. **认证框架** - `auth.py`
3. **任务队列** - `task_queue.py`
4. **多相机注册** - `camera_registry.py`
5. **测量状态机** - `measurement_state.py`
6. **仪器配置** - `instrument_config.py`
7. **监控系统** - `monitoring.py`
8. **配置管理** - `production_config.py`

### ⚠️ 需要注意

- **生产功能尚未集成到app.py**
- 新组件可以独立使用，但未接入现有系统
- 建议先在测试模式下验证现有功能

### 📋 集成TODO

如需完整集成，需要：

1. 修改 `app.py` 的 `db()` 函数使用 `Database` 类
2. 在需要认证的接口添加 `Depends(get_current_user_required)`
3. 将OCR worker改用 `TaskQueue`
4. 启用监控端点

详见 `PRODUCTION_ENGINEERING.md` 第9-16节。

## 验证清单

- [x] 数据库迁移系统工作正常
- [x] Python代码编译通过
- [x] 配置验证可用
- [x] 所有模块可导入
- [ ] app.py集成（需手动完成）
- [ ] 端到端功能测试
- [ ] 生产模式部署

## 监控端点（集成后可用）

```bash
# 健康检查
curl http://127.0.0.1:8188/health

# Prometheus指标
curl http://127.0.0.1:8188/metrics

# 详细状态
curl http://127.0.0.1:8188/api/admin/status
```

## 获取帮助

- 完整文档: `PRODUCTION_ENGINEERING.md`
- 状态报告: `STATUS_REPORT.md`
- 问题反馈: GitHub Issues
