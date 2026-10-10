# 贡献与支持

安装和首次体验从 [README](README.md#quick-start) 开始，代码目录与兼容入口见 [代码结构](docs/CodeStructure.md)，维护约束见 [AGENTS.md](AGENTS.md)。仓库暂未附带 `LICENSE`；许可由所有者另行决定，不应从公开可见推断使用授权。

## 问题与建议

一般问题、文档勘误和改进建议可提交到 [GitHub Issues](https://github.com/kealan-Jun/FieldRecognition/issues)。请给出提交 SHA、平台与 Python 版本、所用 CPU/GPU 模式、最小复现步骤、预期和实际结果，以及脱敏后的错误类型。无需上传整份运行目录或环境文件。项目不承诺固定支持范围或响应时限；敏感安全问题按 [安全说明](SECURITY.md) 处理。

请使用合成图片、临时数据和模型替身复现；真实照片、人员、二维码身份和回执必须先确认可分享并脱敏。不要把密钥、登录会话、设备凭证、NAS 地址与账号或原始生产数据库放入 Issue、PR、日志或截图。

## 提交修改

在独立分支或 worktree 修改，保留无关工作。若主机有运行中的部署，不修改其加载的代码、依赖、配置或静态资源，也不因贡献流程停止服务。提交 PR 说明具体问题、行为变化、验证结果和仍未证明的现场边界；只暂存相关路径。运行变更是否部署另按 [升级守卫](docs/RuntimeReliability.md#升级入口) 判断。

使用本项目 Python 环境，在仓库根目录执行相关测试；运行代码修改完成后执行完整回归：

```bash
env -u FIELD_RECEIVER_URL -u FIELD_CAMERA_SNAPSHOT_URL \
  OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=2 \
  .venv/bin/python -m pytest -q tests
.venv/bin/python -m compileall -q field_recognition scripts tests *.py
for file in static/*.js; do node --check "$file"; done
git diff --check
```

测试使用临时数据，不针对生产 `Data/`。当前 CI 在 Ubuntu、Python 3.11 和 Node 22 上执行上述检查，并使用 Ruff 0.12.0 检查未定义名称和格式；范围见 [检查工作流](.github/workflows/checks.yml) 与 [pyproject.toml](pyproject.toml)。文档修改只需差异、链接、路径与示例命令验证。无需为验证而访问真实相机、NAS、GPU 或云端服务。

保留根兼容入口和既有身份/证据契约；生产代码放在 `field_recognition/`，共享夹具放在 `tests/support/`。历史报告保留其原始版本与结论，当前规则写入维护中的指南。模型置信分数、API 成功或合成测试均不能冒充真实仪器准确率。
