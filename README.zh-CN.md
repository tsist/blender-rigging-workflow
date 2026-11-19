# Blender 骨骼与角色工作台

[English](README.md)

当前预发布 **0.40.1**。在 **Windows / Blender 5.2.1 LTS / 5.3.0** 验证了安装、进入角色、蒙皮与姿态重开以及受限驱动策略。项目代码 GPL-3.0-or-later；第三方代码和参考资源分别保留原许可。

在 **3D 视图 → N → 角色工作台** 进入。已有角色选择骨架并点击“进入角色”，新角色从准备与定位开始。38 个业务面板统一到角色状态、制作阶段和日常／进阶／开发层级，阶段可以直接跳转。

## 安装与技能

从 [v0.40.1 发布页](https://github.com/tsist/blender-rigging-workflow/releases/tag/v0.40.1) 下载：

- `rigging_workflow-0.40.1.zip`：Blender 偏好设置 → 插件 → 从磁盘安装，然后启用。
- `blender-rigging-skill-0.40.1.zip`：唯一配套技能及便携参考说明。
- `blender-rigging-workflow-0.40.1-source.zip`：完整仓库源码。
- `SHA256SUMS`：附件校验值。

不要把完整源码 ZIP 当作 Blender 插件安装。先在独立配置中试用，自行保留旧工程与插件备份。仓库不会自动覆盖日常安装。

## 当前能力

准备与定位、固定 C94 模板拟合与生成、体积扩散蒙皮及局部权重维护；骨输入／Shape Key 共用的面部作者与日常控制；胸臀弹性、发束和裙摆原生 Cloth 残差片段；已支持来源的动画重定向、Profile 与后台队列；随文件手动运行的日常工作台。

**私有 C94 骨架模板、定位图谱、角色、字体、纹理及样本动作不包含在仓库中。** 新角色 C94 生成需自行提供有权使用且满足要求的模板与定位输入。普通已有骨架维护与特定 C94 流程的能力不同，不承诺任意骨架、任意角色一键绑定。

参阅 [安装与输入准备](docs/SETUP.md)、[操作指南](rigging_workflow/USER-GUIDE.md)、[AI 与公共命令](rigging_workflow/AI-CALLS.md)、[重定向指南](rigging_workflow/RETARGET-USER-GUIDE.md) 和 [支持范围](rigging_workflow/SUPPORT-MATRIX.md)。

## 实际验证与限制

0.40.0-r3 在 5.2.1 的既有验收覆盖真实 53,598 点绑定输入、六类已有工程热／冷开、面部、胸臀／侧发／技术裙片、真实后台成功／坏项／取消、实际可见界面，以及最终包原生隔离安装。技术、视觉与素材质量分别判断，详见 [验收说明](docs/VALIDATION.md)。

发布包保持已验收运行代码与资源逐字节一致；少量路径默认值和来源记录仍描述原测试机。换机器时须在现有面板中选择真实 CLI、MMD Tools 与 OpenCC 路径，不会自动下载依赖。包内历史文档的“尚未公开发布”指原本地快照形成时的状态；当前发布以 [公开分发说明](docs/PUBLICATION.md) 为准。

此版本保留为预发布：未认证跨平台运行、任意输入组合、完整素材美术质量、长片连续无穿透或真实 camera VMD 逐帧视觉。暂停的 GLB／密集交换研究没有恢复。源码打包 CI 通过不等于对应系统的 Blender 运行通过。

`rigging_workflow/` 是当前插件，`skills/blender-rigging/` 是唯一当前技能。`work/`、`tools/`、`hrig_paths.py` 和根目录 `SOURCE_MAP.json` 为保留的早期 SDK 历史来源，不要与当前插件混装。

[变更记录](CHANGELOG.md) · [第三方声明](THIRD_PARTY_NOTICES.md) · [许可证](LICENSE)
