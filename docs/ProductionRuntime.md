# 本机托管运行与 NAS 归档

托管模式在 Linux 主机运行 API、相机监控、OCR、归档四个独立服务。OCR 默认 CPU；适配依赖后可显式使用 GPU。NAS 只提供文件共享；原采集目录只读，不安装服务。`FIELD_PRODUCTION_ENABLED=1` 启用服务拆分和持久化任务队列；`FIELD_RECORD_MODE=production` 才启用正式业务草稿确认。两个开关含义不同，测试结果不会自动成为正式记录。

## 服务与入口

| 单元 | 职责 |
|---|---|
| `field-recognition-api.service` | HTTP、网页、Agent 工具；默认免登录；不加载模型或连接相机 |
| `field-recognition-cameras.service` | 从登记表读取已登记使用人的相机；每台相机一个监控进程 |
| `field-recognition-ocr.service` | 统一 OCR 模型所有权；照片任务优先、按相机公平取任务；最多四个照片流程等待，模型串行 |
| `field-recognition-archive.service` | 消费本地归档队列、同步 NAS、生成索引与完整性巡检 |
| `field-recognition.target` | 一起启动、停止上述服务 |

进程通过本机权限为 `0600` 的 Unix socket 通信，不开放额外网络端口。API 默认只监听 `127.0.0.1:8188`，可通过 `FIELD_HOST`、`FIELD_PORT` 调整；默认免登录应保留本机监听。打开对应地址即可使用；免登录模式下旧 `/login` 地址返回工作台。

迁移前使用 SQLite backup API 生成一致性备份。迁移只增加结构或保留原表副本，不删除照片、绑定、历史记录。以下是新建离线部署或已获明确授权停写窗口的安装步骤；正常使用期间的迭代不能执行停服或原地安装。相机采集进程停止会结束旧绑定，恢复后须重新扫码，进行中的采集会中断；已经保存的照片、队列与历史回执保留。不要为满足部署前提主动制造停机。

## 首次离线安装

先按 [README](../README.md#quick-start) 建立本项目环境。需要 Linux 用户 systemd 会话；终端应保留其 `XDG_RUNTIME_DIR` 和 D-Bus 环境，不使用 CPU 体验的 `env -i` 命令安装服务。安装器从实际仓库位置生成单元，不要求固定主目录或 clone 路径。

在仓库根目录创建本地配置，已有 `.env` 会保留：

```bash
test -e .env || (umask 077; cp .env.example .env)
chmod 600 .env
```

编辑 `.env`，填写部署自己的相机和来源；配置 Receiver 或快照 URL 时必须提供精确 `FIELD_CAMERA_ID`。真实业务至少设置 `FIELD_RECORD_MODE=production`、`FIELD_REGISTRY_POLICY=none`，按需要启用照片监控与归档。默认 CPU 环境无需 GPU。数据与数据库路径可分别用 `FIELD_DEMO_DATA`、`FIELD_DATABASE_PATH` 设置，使用本机存储；文件内路径写实际值，不使用 shell 变量展开。

托管采集会为已登记并分配使用人的相机启动采集进程，必须有可用 Receiver 主码流来源；仅配置快照 URL 不能提供该采集能力。无硬件首次体验或只有已有照片的环境使用 README 的单进程上传流程。以下为需替换的配置实例：

```dotenv
FIELD_RECORD_MODE=production
FIELD_REGISTRY_POLICY=none
FIELD_CAMERA_ID=sender_cam01
FIELD_RECEIVER_URL=http://receiver-host:8080
```

<a id="owned-registries"></a>

### 登记自己的仪器与场景

新建正式库通过准备脚本显式导入自有注册表。创建本地目录：

```bash
mkdir -p Data/Registries
```

将下面的结构保存为 `Data/Registries/Instruments.json`。**示例 UUID 只展示格式，必须换成自己实际仪器标签载荷中的 ID，不能把它当作真实仪器。** 新制标签应使用自己的唯一 UUID，并让注册表与标签中的 `{"v":1,"type":"instrument","id":"同一UUID"}` 一致；标签印好后仍需真实相机解码核验。

```json
{
  "schema": "FieldRecognitionQrLabels/1",
  "instruments": [
    {
      "instrument_id": "00000000-0000-4000-8000-000000000001",
      "label": "自有仪器"
    }
  ]
}
```

仪器必填字段为有效 UUID `instrument_id` 和非空 `label`（最多 80 字符）。可选 `measurement_ranges` 声明已经核对的字段单位、量程和显示规则；`type_id`、`device_category`、`scene`、`model`、`device_no` 分别声明类型、类别、登记位置、型号和编号。未知值留空，不从样例补造；`scene` 仅是登记位置文字，不证明扫过场景码。

场景为可选的独立登记。需要时保存 `Data/Registries/Scenes.json`，将示例 UUID 和名称换成自己的场景；标签载荷为 `{"v":1,"type":"scene","id":"同一场景UUID"}`。没有场景标签时可省略该文件及后续 `--scene-registry` 参数。

```json
{
  "schema": "FieldRecognitionSceneQrLabels/1",
  "scenes": [
    {
      "scene_id": "00000000-0000-4000-8000-000000000002",
      "scene_name": "自有实验台"
    }
  ]
}
```

场景 ID 必须是 UUID；仅有非空 `scene_name` 的项会导入。`Data/Registries/` 随 `Data/` 被 Git 忽略，自有身份资料不提交仓库。参数支持绝对路径，相对路径按本项目根目录解析；自定义数据目录也可存放这些文件，但需传实际路径。导入只登记元数据，不产生 `matches`、扫码照片或绑定；JSON 中的 `decode_verified` 等描述不能代替实际解码证据。

确认没有本项目运行中的写入进程后执行，下列人员与相机值是占位实例，需替换：

```bash
.venv/bin/python prepare_runtime.py --env-file .env \
  --username operator01 --display-name 实验员一 --camera sender_cam01 \
  --receiver-url http://receiver-host:8080 --registry-policy none \
  --instrument-registry Data/Registries/Instruments.json \
  --scene-registry Data/Registries/Scenes.json
.venv/bin/python scripts/install_runtime.py --env-file .env
systemctl --user status field-recognition.target
```

准备脚本默认不导入样例；体验环境可明确传 `--registry-policy sample`，不能与自有注册表参数同时使用。显式自有文件与 `--registry-policy none` 可同时使用；重复导入只添加缺失 ID，不覆盖既有名称、编号、规则、身份或证据，也不用于修改已有登记。运行时 `FIELD_REGISTRY_POLICY=auto` 仅在 test 模式导入样例，production 模式不导入；`none` 不删除既有登记。准备脚本支持 `--database-path` 覆盖数据库路径；准备、安装与运行必须指向同一库。启动后仍需从实际二维码解码建立新绑定，导入文件本身不授权使用关系。

安装器检查旧单进程及托管单元，运行中或状态无法确认时拒绝，绝不自动停止/重启。首次机器没有旧单元正常跳过；已离线的旧单元可禁用自启，不调用 `--now`。缺少 `.env` 默认报错；`--init-env` 仅在缺失时以 `0600` 创建模板，不覆盖已有配置，仍须核对配置与准备数据库。`--no-start` 安装并启用自启但不立即启动。已有部署必须先满足上述离线或明确停写授权条件，不能通过安装器制造停机。

默认 `FIELD_AUTH_ENABLED=0`，网页、API 和 Agent 均无需登录。准备脚本登记使用人唯一 ID、姓名与相机关系，不生成密码；已有账号、绑定身份、原图和历史回执保留。实验员登记仍用于数据归属，免登录下的操作人是本机登记或请求声明的身份，不代表通过账号认证。

一台相机可以登记多名人员。`camera_users` 以 `(camera_id,user_id)` 为联合主键保留成员关系，`camera_active_users` 单独保存当前使用人，后台只为每台相机启动一个采集进程。重复运行准备脚本会追加成员并切换当前使用人；此前成员、绑定身份和二维码证据保留。切换时同步更新有效关系与后台实验员设置，并记录人员变更历史。

`PUT /api/admin/cameras/{camera_id}/owner` 提交 `{"user_id":"已登记人员ID"}`，追加成员并切换当前使用人；`GET /api/admin/cameras/{camera_id}/members` 查询成员；`DELETE /api/admin/cameras/{camera_id}/members/{user_id}` 移除非当前成员。默认本机免登录可调用，开启登录时要求管理员。相机成员都可在登录后访问该相机；设备凭证及后台采集使用当前使用人。成员姓名重复时，绑定或自动运行接口需提供明确的 `wearer_id` 才能归属到人员 ID。

二维码身份按活跃绑定独占：同一个二维码不能同时绑定到多个设备或相机。明确结束绑定（含交接交出）、当天凭证失效、设备离线、采集服务异常或 Receiver 接入会话变化都会释放旧占用；另一相机可凭新的实际扫码建立自己的关系。异常观测不等于已证实设备物理关机，旧绑定与原始扫码证据不会删除。完整规则见 [自动解绑](AutomaticBindingRelease20260921.md)。

登录是独立的可选配置，托管模式或 `FIELD_RECORD_MODE=production` 均不会自动开启。新建需要账号认证的部署时，设置 `FIELD_AUTH_ENABLED=1` 并使用准备脚本的 `--enable-login`；初始密码只写入权限为 `0600` 的 `<数据目录>/Access/InitialAccess.json`。默认登录地址按 `FIELD_HOST`、`FIELD_PORT` 生成，受控网关部署可用 `FIELD_LOGIN_URL` 或 `--login-url` 明确指定。`FIELD_SESSION_DURATION_HOURS` 同时控制会话及 Cookie 时长（1–72 小时，默认 8）。仅在登录配置下启用会话、CSRF、相机账号隔离及管理员账号接口；不要把凭证交付文件上传仓库或截图公开。

`/api/v1/` 与现有 `/api/` 执行相同业务规则。`GET /api/cameras` 列出已登记相机，后续请求使用 `X-Camera-Id` 选择相机。Agent 工具目录见 `GET /api/tools`，测量与交接工具直接调用同一业务事务。

## 确认、交接与恢复

生产 OCR 结果进入测量草稿。一次连拍由 `burst_id` 合并，同一来源的网络重传复用照片、任务和测量。校正保留原字段证据和修订版本；确认校验目标、冲突、版本后写入正式记录，并读回验证。`test` 模式仍仅生成测试产出。

同一相机当天内可直接更换操作人，无需人工先结束绑定。向 `POST /api/bindings` 提交同一仪器的有效扫码记录和新操作人时，在同一事务内结束旧人员使用时段并创建后继时段，分配新的 `binding_id` 和开始时间，记录前后继引用与 `session_revision`；当天有效期、`qr_hash` 与原扫码证据保留，旧时段和归档版本不改写。全相机交接可通过当前使用人接口或自动运行设置为全部有效关系创建后继时段。单次绑定仍记录一名明确操作人；相机的多人登记用于共享设备与人员切换。

仍被有效绑定占用的仪器不能由另一相机静默接管。主动交接时，接收人需实际扫码申请，原使用人明确交出后，接收人确认；请求跨过北京时间零点即失效，必须使用当天新的二维码证据。设备离线或采集异常已释放占用时，另一相机可直接重新扫码，无需旧使用人确认。晚到照片按拍摄时间匹配历史使用时段，无法可靠归属时保持待复核，不改写旧归属。当天换人不延长北京时间次日零点的到期时间。

任务队列保存在本机 SQLite，包含租约、尝试次数、重试时间和失败历史。60 秒租约、15 秒续约；进程退出后过期任务可重新领取，旧租约不能覆盖新结果。暂时故障采用指数退避和抖动，最多初次加三次重试；耗尽进入失败队列，填写操作人 `actor` 和理由 `reason` 后重放（启用登录时使用管理员账号身份），正式确认记录不允许重放覆盖。相机与全局队列有容量上限。

NAS 断开或容量不足时暂停归档写入，本地事务队列保留；恢复后重试。归档写入校验源文件和目标哈希，先原件后回执，成功后才确认本地队列。NAS 错误不阻塞 API/GPU；不自动清理历史材料腾空间。

## NAS 目录

打开 `FieldRecognitionArchive/Readme.html` 查看中文入口，再按日期和相机查看事件。目录名称采用 PascalCase，记录 ID、日期和哈希沿用原始标识。以下为结构示例，完整布局和字段规则见 [NAS 文件夹与留存指南](NAS文件夹与留存指南.md)。

```text
FieldRecognitionArchive/
  Readme.html                            中文导航
  <日期>_<相机ID>/
    Bindings/<事件>/Binding.json          扫码、绑定、场景与交接证据
    VideoReadings/<采集>/                 视频观测
    VoicePhotoReadings/<测量或采集>/       语音照片及测量版本
      Result.json                        单台仪器六字段结果；有合规结果时生成
      Evidence.json                      原图、时间、身份、候选及决策证据
      Photos/                            原图
      Regions/                           已定位识别区域
    PhotoReadings/                       兼容上传照片；有数据时出现
    DailyReport/                         同日同相机事件导航
  .System/
    Receipts/                            不可覆盖的历史版本
    Integrity/                           完整性巡检
    MigrationMap.json                    旧路径映射及稳定事件目录
    Index.json                           全量版本索引
    Audit.html                           历史版本阅读入口
```

同一测量的连拍、修订和确认共用稳定事件目录，`Evidence.json` 保存原件、观察及决策；一图多仪器使用 `Result.json`、`Result02.json` 分别输出，不能把证据包装当作规范读数。同一哈希原件只存一份，通过相对路径引用；`.System/Receipts` 保留不可覆盖版本。旧 `Browse/Records/Objects/Receipts` 不再作为当前可见业务结构，旧 HTTP 地址通过迁移映射继续读取。未知文件、人工备注及原回执保留；备份必须包括隐藏的 `.System`。

## 运维与验证边界

更新代码统一使用 [运行可靠性说明](RuntimeReliability.md) 中的 `scripts/upgrade_runtime.py`。
先在独立 worktree 测试提交，再用 `--revision <SHA>` 检查候选。正常使用期间没有经验证的运行代码不中断替换路径，运行变更保持待部署；守卫要求离线不构成停服授权。仅真正离线或已有明确停写授权时，才能在保存服务身份、绑定与队列状态后切换代码、演练迁移并备份。
安装新的用户单元后，四个服务在启动时都等待 `field-recognition-prepare.service` 成功。

- `/health/live` 检查 API 存活；`/health/ready` 检查数据库及必要工作进程心跳。相机画面在线与真实识别是否成功需查看状态及业务证据。
- `/api/admin/status` 查看进程、OCR、队列和归档；`/metrics` 输出队列与待归档数量；默认可在本机直接查看；启用登录时要求管理员身份。
- 相机采集进程退出、重启或失去响应会结束该相机旧绑定；设备离线、采集会话变化、北京时间跨日、显式交接或手动结束同样释放关系。恢复后需重新扫码，晚到照片保留拍摄时的历史归属。
- 原有模型 A/B 的明确资产映射继续使用；通用设备类型检测只能说明类型，不能仅凭“当前只有一台同类绑定”推断物理资产。
- 自动测试使用临时数据库，覆盖身份隔离、实际 API 到工作进程链路、事务、重传、租约、重试及归档去重；这些证据不代表现场设备识别准确率或整套生产上线验收。

回退同样需要真正离线或明确授权的停写窗口，先保存新产生的数据库、队列及绑定状态。不得直接用升级前备份覆盖已经产生新记录的数据库。需要旧程序应在隔离目录验证兼容后恢复运行，证据目录保持原位；被释放或过期的绑定不能从备份恢复成当前有效关系。
