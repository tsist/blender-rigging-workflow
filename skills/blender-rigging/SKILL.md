---
name: blender-rigging
description: 使用同一角色工作台维护人形骨架、体积蒙皮、面部控制、辅助动态和已支持来源的动画重定向，交付可编辑原生Blender工程；完整新角色C94流程需另行提供授权模板与定位图谱，不承诺任意控制图或GLB往返。
---

# Blender 骨骼与角色工作台

配套运行包为 0.40.1，公开仓库[blender-rigging-workflow](https://github.com/tsist/blender-rigging-workflow)，预发布版 0.40.1。本技能是唯一便携入口，不携带原工作站、角色或私有审计。Windows Blender 5.2.1 与 5.3.0 的基础工作台和受限驱动已实测。人工、AI与公共CLI共用运行核心；不要安装历史SDK目录冒充当前产品。

## 先确认目标与环境

恢复当前任务及用户要求，分清新角色、已有C94、普通骨架维护和重定向。只恢复当前明确授权工作，不从旧pending记录或保存的token恢复提交权，不默认重启GLB／密集交换研究。

基线为Windows／Blender5.2.1 LTS。当前场景先核对正确Blender Lab会话、文件、Scene、目标、模式和共享数据；磁盘工作副本按公共blenderctl当前契约执行。输入SHA、资源、driver profile、输出和异步终态以实际工具为准，不能编造rigging专用命令。

当前C94包使用公共 `extension.run` 与精确包身份。历史 `rigging-workflow` locator可能指另一个旧产品，不能用它的成功冒充当前包已被调用，不静默改共享locator。实际API和参数见 [AI-CALLS](references/AI-CALLS.md)。

完整新角色生成需另行提供授权模板和定位图谱，见 [模板契约](references/TEMPLATE-CONTRACT.md)。插件不包含私有角色／字体／纹理／样本动作，不自动下载依赖。新机器须在文件导入／可选镜头面板明确选择实际CLI、MMD Tools与OpenCC路径。

## 人工入口与阶段

3D视图N → 角色工作台。已有角色选择骨架 → 进入角色；新角色从准备与定位开始。阶段可自由跳转，只阻止前置不足操作；日常提供主要操作，进阶完整参数，开发集中诊断。

1. 输入检查：部件、旧绑定、有效表面、单位／朝向、依赖和独立工作副本。
2. 模板／模块：先选齐角色模块，确认整体参考结构；后加模块走显式返工。
3. 定位／拟合：自动坐标候选、手调／锁定、指定未锁域重算，查看叠图后确认完整参考骨。
4. 生成／蒙皮：生成完整C94机制，再体积扩散、服装转移或独立求解、发裙链及刚性附件；用真实姿态核查。
5. 面部：权重复核、骨输入混合设计、整组采样角色Key、修正／组合；已有Key保留或显式映射。
6. 动态：胸臀弹性，独立发裙后端与碰撞；预卷／捕获／预览／混合／停止／重置／冻结／烘焙。
7. 动画／维护／交付：按已支持来源设置Profile与队列；保存可编辑.blend、日常Text和依赖，独立重开。

模板、参考层、整块效果按当前任务需要提供实际预览。已经明确授权且未失效的同角色决定复用；用户暂缓的视觉精修记deferred，不反复索取也不写成通过。原骨架局修按实际影响裁剪，不强加无关新建流程。

## 数据保护与故障恢复

源文件保持，重要修改先独立检查点；Undo不替代备份。核对对象／Data共享、Action/Slot/NLA、Key、权重、约束／driver及非目标。只清理当前操作明确拥有的still-live新ID，不用全局ID差集删除外部callback或用户新增数据。

局部同步不解除全局fit_stale。配准只提议坐标，不改变固定拓扑或架构；ROI失败保未估计，不把初始化伪标manual。移动参照后重新应用。取消／失败不提交部分权重，不用最近骨或全组归一化掩盖失败。

默认真正体积扩散。Heat代理、转移或技术包络如实标识；临时封口／加厚／分区代理不能替换生产拓扑／UV／Key。明确目标、可写点／过渡带、deform组和锁组。候选存在或全点有权重不等于姿态美术通过。

局部volume／绘制只在当前支持的Key／driver条件内工作。简单driver定义、映射或输入变化即过时；不删Key绕过，不扩大成任意姿态或带Key拓扑派生。需作者返工时保无Key采样源，修权重后重采受影响项。

## 面部、模式和日常控制

骨控、独立图形板和中文侧栏共用状态；先混合骨输入再整组采样，不以Key相加冒充。改权重后重采受影响Key；骨架／中立不变才复用骨输入。切换真实KEY/BONE前检查同rig各hub的raw输入及动画，显隐不代替输入校验。

Key修订保Basis、其它Key、源数据；完整pose／JSON／显隐失败回滚。manual Key重绑更新中立与下游指纹。旧默认两点恒等driver保留兼容，非恒等曲线不删除点绕过。

区分直接模式属性、保姿态snap和动画记录；RESET／KEY仅选中控制变换，匹配只当前帧且需更早源键，不自动补旧帧。新Action保原Action／slot和非目标。受driver控制的IK/FK若不能实际切换，明确拒绝并恢复，不能假成功。

## 辅助动态

胸臀：`elastic.install/configure/capture`与Clip的preview/bake、freeze/reset/stop_preview。先采完整基础／预卷再写新helper四元数Action，保Base Action／Slot／骨／权重。烘焙核实际Action、Slot和全部写域；foreign NLA须明确处理，源或参数变化重新capture。

发裙：`surface_dynamic.install/configure/capture`及对应Clip入口。明确根部正权pin和碰撞Collection，同source一组，组件及原非面点明确锚点。原生Cloth先采全基础／实际碰撞轨迹，再逆LBS写独立relative Mesh Key残差与时钟Action；不称骨烘焙，不删RAW或豁免碰撞。

当前限本地普通LBS／可选Subsurf、无Key源／碰撞输入及明确同rig依赖。胸臀先原生烘焙再捕获发裙；活跃RAM、外来逐帧回调或未跟踪driver／父级写前拒绝，不卸外来代码。预览固定片段，输入变化重捕获；保存未烘焙预览回基础，冻结保残差，reset只本组回基础，失败恢复session／显示／新ID。

胸臀spring不替代发裙Cloth。短片段数值通过不签完整生产发型、裙或连续无穿透。

## 重定向与队列

只按 [支持表](references/SUPPORT-MATRIX.md) 与 [动画指南](references/RETARGET-USER-GUIDE.md) 选择C94／普通FK／Profile／MMD等路径，源NLA最终合成未实现。明确来源、目标、Profile、独立目录和每条clip身份；重复resolved VMD路径应明确拒绝，不静默漏条目。

提交、状态、取消、收集、预览、STOP恢复与释放走同一共享会话；取消保留已成功项，终态后核输出。跨Scene副本UID须为本目标重新转换；同一Object链接多个Scene不等于独立副本。失败保目录、阶段和cause，不擅自删快照。

可选镜头需显式第三方路径与文件身份；旧camera Profile保原JSON／库，凭expected SHA迁移新sidecar，不静默补签。保存文件不恢复临时批准、collect或live提交权。

## 验证与交付

实际输入 → 共享核心 → 真实入口 → 可观察结果 → 保存／独立重开。根据改动和故障补受影响检查，复用仍有效证据；不因交接重跑昂贵求解。技术、视觉、便携和交换分别标pass/fail/not_run/deferred，文件存在、骨数、FINISHED或注册成功不代替效果。

便携工作台由同源13日常模块构建，Text手动执行，不开autoexec。真实引用、UID／Scene门与面部setter/layout/record保留；制作作者／蒙皮／实时求解仍留在插件。字体显式用目标blend目录相对路径，保存冷开核解析。

新建PoseBone属性driver首次invalid时核original/evaluated路径和真实生产初始化步骤，不强设valid或改autoexec。原生driver帧、旧场景范围门需要显式迁移或重烘焙，不能只更新插件就宣称旧输出修好。

交付可编辑.blend、明确依赖、简明变更、真实验证、手修限制和恢复入口。全片GLB／FBX、超密集Key交换及任意连续时刻一致性不属默认要求；历史BBONE_SHARED_CONTINUOUS_V1／SHARED_RIG_NATIVE_TIME_V1研究不自动恢复。有新格式目标先明确损失项和授权。

本版已验范围见 [VALIDATION](references/VALIDATION.md)，实际操作见 [USER-GUIDE](references/USER-GUIDE.md)。未知角色或素材艺术问题不得据既有样本擅自标通过。
