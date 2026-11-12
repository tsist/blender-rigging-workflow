# AI、GUI 与公共命令调用

GUI 与 AI 调用同一包内领域核心，不维护第二套绑定、表情、动态或重定向算法。当前包为 C94 `rigging_workflow`；项目公共 `rigging-workflow` 命令的旧产品定位器指向另一棵带 `contracts.py/core/service.py` 的历史产品，不能用该命令的成功证明本 C94 包被调用，也不改定位器影响其它任务。

## 当前可用入口

- 当前打开的 Blender：在核对文件、Scene、View Layer、模式和明确对象后，经 Blender Lab MCP 调用同包核心，或调用已注册原生 operator。实际保存到新工作副本。
- 磁盘工作副本与隔离验证：公共 `blenderctl extension.run` 执行明确本地 Python 脚本，导入精确包副本后使用相同核心。本轮真实回归采用此入口。
- 包内体积蒙皮和重定向后台作业：由对应核心建立、查询和取消自己的 job。不要绕开其输入指纹、提交权限或 package manifest。

`extension.run` 是已授权本地 Python 执行，不是操作系统隔离。当前公共 CLI 0.85.0 / Blender 5.2.1 的资源、输出与作业契约保持；所有资源须给绝对路径及真实 SHA。AI 先核当前 Schema，不假定历史版本相同。

## 同一能力的人工与 AI 对照

| 工作台阶段 | 共享源码入口 | 调用原则 |
|---|---|---|
| 准备与定位 | `workspace_setup.append_atlas/prepare_positioning`、`positioning.prepare/estimate/fit` | 明确完整参考、图谱、目标和中立框架；原源保持 |
| 模板与模块 | `module_configuration.prepare/validate`、`character_generation.generate` | 使用同一配置、模板及参考身份，不手改签名放行 |
| 完整角色绑定 | `workflow_entry.prepare` 返回 `PreparedEntry`；`capture/start_volume/start_plan/bind_transfer/bind_rigid/finish/abort` | 返回 live 对象持有提交权；磁盘 JSON 不能恢复 RAM apply 权限 |
| 局部蒙皮与维护 | `skinning_policy`、`local_skinning_job`、`weight_paint`、`native_maintenance`、`author_rework` | 逐项明确 Mesh、骨范围和权重来源，保非目标数据 |
| 控制与记录 | `pose_switching.switch`、`pose_switching_leg.switch`；`mode_animation.record_modes/record_limb/switch_and_key` | 匹配、单改模式、写关键帧是不同动作，需按任务选择 |
| 面部制作与日常 | `face_modes`、`face_pose_library`、`face_capture_group`、`face_key_revision`、`face_daily` | 明确 rig/hub、真实对象引用、中立记录与Key身份；使用原核心验证 |
| 胸臀及发裙 | `elastic.capture`、`surface_dynamic.capture` 返回片段的 `preview/bake`；各自 `stop_preview` | 参数/基础动作变更后重捕获；不从旧片段改签名恢复 |
| 动画候选 | `retarget_action.convert/convert_resolved/begin_preview/stop_scene_preview`，来源适配器和 `retarget_pipeline` | 低层 convert 只生成候选；产品只读检测编排另按当前调用链接入 |
| 正式使用 | `retarget_publish.commit/withdraw` | 只有已审阅候选才能提交；额外接触候选仍走逐问题批准入口 |
| 后台队列 | `retarget_job` 及各来源 job 模块 | 保留请求、package/source 指纹、独立输出和回执；完成后再 collect |

签名、参数与返回值以这些文件的当前定义为准；表格是入口索引，不替代各接口的前置条件。未实现的默认回退、源 NLA 最终合成、任意控制器与已暂停交换研究不能借任意 Python 冒充支持。

## 构造一次公共调用

用 UTF-8 JSON 提交 `schema_version: "1.0"`、`command: "extension.run"`。`params.resources` 列出脚本、精确代码 ZIP、输入 `.blend` 和实际使用的模板/配置资源及各自真实 `expected_sha256`；`params.manifest` 使用本次授权的 `trust: "execute_local_python_without_sandbox"`、脚本描述、业务参数和独立输出文件名。用本批 `--jobs-dir`，全局参数放在 request 前。

脚本在本 job 目录解包代码，确认不存在其它路径的同名已导入包，再导入该副本。打开源时禁止自动执行，业务修改只保存 job 中新输出。即使顶层 `ok=true`，仍回读领域输出中的错误、检查结果与实际产物；测试脚本完成不等于流程或美术验收通过。

声明输入列表不是依赖完整性的自动证明。保留贴图、缓存、库链接和原相对路径语义；交换、发布、安装和对外发送分别遵守当次授权。

## 接手与恢复

记录包版本及 manifest SHA、输入与输出路径/SHA、明确对象名、Action/Slot、帧域、job目录及未验项。保存文件不恢复 live 制作会话、临时预览或逐问题批准。外部修改使指纹失效时重新取证，不改旧 SHA、清用户数据或从其它入口绕过拒绝。
