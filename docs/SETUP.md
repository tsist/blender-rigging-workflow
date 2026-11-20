# 安装、输入与可选依赖

在独立 Blender 配置中，从磁盘安装并启用发布页的 `rigging_workflow-0.40.1.zip`，然后打开 3D 视图 N → 角色工作台。软件安装与常用入口已在 Windows Blender 5.2.1 与 5.3.0 测试。

已有 C94 工程选择目标骨架 → 进入角色；新建 C94 角色须先提供授权的模板 .blend 和定位图谱，明确目标 Mesh、部件、单位、朝向和中立姿态。模板、图谱、角色、字体和样本动作不随本仓库提供。不要把任意 Rigify、Snow 或其他控制图冒充受支持的 C94 模板。普通 FK 或已有绑定只按其已支持路径使用。

[使用指南](../rigging_workflow/USER-GUIDE.md) 和 [技能模板契约](../skills/blender-rigging/references/TEMPLATE-CONTRACT.md) 给出输入及操作边界。

## 路径和后台执行

运行包保留原验收快照。来源记录中的绝对路径不是公开可用资源，也不会自动下载。文件导入／可选镜头面板的 MMD Tools 与 OpenCC 路径（Windows 另需 CLI 路径）必须改成自己的真实路径；默认的原测试机路径不能直接复用。始终指定独立工作目录并保留失败诊断。

已有原生 Action 的预览不需要临时启用 MMD Tools。PMX/VMD 文件导入或可选镜头后台构建需自行已有：

- Windows 使用公共 blenderctl；原验收使用 0.85.0 与 PowerShell 7。Linux 的以下两个文件入口不需要它。
- MMD Tools 4.5.14，以及 OpenCC-python-reimplemented 0.1.7 wheel。具体文件身份门见运行源码及依赖声明。
- 有权使用的输入文件和完整依赖；输入路径、预期 SHA 和资源清单由各入口提交并校验。

插件不安装、升级或热启用第三方依赖，不保存全局自动执行偏好。请使用 [blenderctl](https://github.com/tsist/blenderctl) 当前帮助核对 `extension.run`；历史 rigging-workflow locator 不能代替当前 C94 包，见 [AI-CALLS](../rigging_workflow/AI-CALLS.md)。

## 源码与回退

`rigging_workflow/` 可作为当前源码包；不要同时启用旧 humanoid_rigging_probe 充当同一产品。旧目录仅供历史研究，原 v0.1.0-dev1 标签与附件保留。

更新前保存 .blend 和旧插件副本。回退时在关闭本任务临时预览／后台任务后停用新插件，恢复自己的旧插件与工程备份；不要用旧版本覆盖新格式工程的唯一副本。新旧 camera Profile 使用显式 sidecar 迁移并保留原文件，不静默补签。安装和版本切换本身不授权提交保存的内存会话。

## Linux PMX/VMD 来源面板

Linux Blender 5.3 的 PMX/VMD 来源面板可直接启动同一 Blender 的全新无界面子进程，不需要 PowerShell 或 Linux standalone blenderctl。请明确指定新输出根、PMX/VMD（或明确接受 Uka 标准参考源）以及已核的 MMD Tools / OpenCC 依赖。当前 GUI 场景不会打包给子进程；作业成功并校验输出后，仍需手动点「追加完整来源并载入当前场景」。失败和取消保留私有作业目录中的诊断，查看 `job/worker.log`。

该入口只执行随本插件提供的来源导入 worker，不开放通用 `extension.run`。只接受可信本地输入及用户选择的可信 MMD Tools；后台 Blender 不构成操作系统安全沙箱。Windows 继续使用原公共 CLI 路径。完整新角色 C94 流程不在此验证范围。

Linux 读保护要求输入、后台输出目录和其完整父路径位于受支持的本地文件系统（ext2/3/4、XFS、Btrfs 或 tmpfs），并支持 Linux 文件租约及 inotify。Overlay、网络盘、FUSE 等不合格路径会拒绝导入，不降级成仅校验哈希。可在合格卷上使用私有副本。

可在受支持文件系统上的私有工作副本中运行 `RIGGING_TEST_ROOT`（已存在的临时目录）和 `RIGGING_MMD_TOOLS`（已核插件路径）指向的现场验收：`blender --factory-startup --background --disable-autoexec --python-exit-code 1 --python scripts/smoke_linux_mmd_import.py`。验收生成合成 VMD，仅在本地读取项目随附、保留署名的 Uka 参考 PMX；不会安装插件或切换配置。

## Linux MMD VMD 相机面板

Linux Blender 5.3 的可选 MMD 相机面板使用专用后台进程。选带相机轨道的 VMD、MMD Tools 4.5.14、OpenCC wheel 和一个已存在的私有作业目录；文件和目录需要符合上述 Linux 读保护要求。以当前场景的 FPS 构建时，仅传递帧率，不传递当前工程或场景。成功后依次点「收集成功结果」和「Append 原生相机与 Object/Data Action」。构建、收集和 Append 都不切换活动镜头，不启动预览或渲染。

取消或失败不开放成功结果。请在提交的 Blender 会话中收集结果；重启不会接管旧作业。诊断保留在作业子目录的 `worker.log`。此入口不开放通用 `extension.run`，不替代 Windows 作业路径。

现场验收沿用上述两个环境变量：`blender --factory-startup --background --disable-autoexec --python-exit-code 1 --python scripts/smoke_linux_mmd_camera.py`。脚本生成合成相机 VMD，并检查 Append 后另起进程载入 .blend 的结果；无需原工程素材。

以上 Linux 入口说明适用于包含 Linux 修复的源码构建；现有 0.40.1 发布附件和原验收记录保持不变。
