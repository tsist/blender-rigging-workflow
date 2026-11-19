# 安装、输入与可选依赖

在独立 Blender 配置中，从磁盘安装并启用发布页的 `rigging_workflow-0.40.1.zip`，然后打开 3D 视图 N → 角色工作台。软件安装与常用入口已在 Windows Blender 5.2.1 与 5.3.0 测试。

已有 C94 工程选择目标骨架 → 进入角色；新建 C94 角色须先提供授权的模板 .blend 和定位图谱，明确目标 Mesh、部件、单位、朝向和中立姿态。模板、图谱、角色、字体和样本动作不随本仓库提供。不要把任意 Rigify、Snow 或其他控制图冒充受支持的 C94 模板。普通 FK 或已有绑定只按其已支持路径使用。

[使用指南](../rigging_workflow/USER-GUIDE.md) 和 [技能模板契约](../skills/blender-rigging/references/TEMPLATE-CONTRACT.md) 给出输入及操作边界。

## 路径和后台执行

运行包保留原验收快照。来源记录中的绝对路径不是公开可用资源，也不会自动下载。文件导入／可选镜头面板的 CLI、MMD Tools 与 OpenCC 路径必须改成自己的真实路径；默认的原测试机路径不能直接复用。始终指定独立工作目录并保留失败诊断。

已有原生 Action 的预览不需要临时启用 MMD Tools。PMX/VMD 文件导入或可选镜头后台构建需自行已有：

- 对应环境的公共 blenderctl；原验收使用 0.85.0 与 PowerShell 7。
- MMD Tools 4.5.14，以及 OpenCC-python-reimplemented 0.1.7 wheel。具体文件身份门见运行源码及依赖声明。
- 有权使用的输入文件和完整依赖；输入路径、预期 SHA、资源清单按实际 CLI 契约提供。

插件不安装、升级或热启用第三方依赖，不保存全局自动执行偏好。请使用 [blenderctl](https://github.com/tsist/blenderctl) 当前帮助核对 `extension.run`；历史 rigging-workflow locator 不能代替当前 C94 包，见 [AI-CALLS](../rigging_workflow/AI-CALLS.md)。

## 源码与回退

`rigging_workflow/` 可作为当前源码包；不要同时启用旧 humanoid_rigging_probe 充当同一产品。旧目录仅供历史研究，原 v0.1.0-dev1 标签与附件保留。

更新前保存 .blend 和旧插件副本。回退时在关闭本任务临时预览／后台任务后停用新插件，恢复自己的旧插件与工程备份；不要用旧版本覆盖新格式工程的唯一副本。新旧 camera Profile 使用显式 sidecar 迁移并保留原文件，不静默补签。安装和版本切换本身不授权提交保存的内存会话。
