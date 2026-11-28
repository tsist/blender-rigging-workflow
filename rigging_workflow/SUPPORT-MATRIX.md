> 0.42.3-rc1：以下保留历史功能原文，不能作为本RC已运行或完整质量通过证明。当前操作/范围/恢复见[用户指南与回退](用户指南与回退.md)、[支持矩阵](支持矩阵.md)、[AI同核调用](AI同核调用.md)及[RC状态](RELEASE-CANDIDATE.md)。

# 0.41.0 本机候选支持范围

## 0.42.2统一候选说明（2026-11-13）

0.42.2统一候选包含中立采样/片段起点修订，组合通过与not_run以包外报告为准。7317点人形的驱动总数为Object89+Armature-data8=97；没有驱动减少。肩胯、连续碰撞、全部来源/完整ROM仍不获签，已拒提案不集成。



## 本批低模与局部检查约定

本批Tripo样本采用Tripo云端重拓扑低模；原高模保留作来源和外形对照，既有高模实证保持各自范围，不代签低模。生产检查先继承已保存的普通蒙皮权重，明确记录TRANSFER；不在本地重拓扑，不反复全身重算。

肩胯问题须在相同视角和0–1色阶下并排比较相关各变形骨及辅助骨的权重，结合相关组总和、选点权重表与骨架视图，沿控制输入→ORG/MCH→最终变形骨/helper定位。按实际use_deform和消费关系判断骨职责，不能只看名称、最终几何或旋转角度幅值。先固定一层原因，再对明确点域/组做局部修复并复验；保留原候选和回退。

多Object身体的代理回写增加几何来源过滤：只保留当前来源三个顶点均归属且有体素支持的完整三角；任何生产点无完整有界覆盖时拒绝整个候选，不自动扩大距离。该方法是顶点几何近似，不能消歧完全同位置重叠，也不证明三角内部语义归属。


> 本地候选（2026-11-12），未替换日常安装或公开发布。下部保留0.40.0旧支持表；017/018等是本轮历史证据快照。当前完整范围、已知视觉限制及包验收状态以本表、本批审计报告和交付索引为准。

## 模块化候选范围（历史核心证据与本轮增量）

020流程增量：019可见UI整链已完成并保存，独立CLI冷开11项通过（`A/jobs/bb379155898c42ac9eeb5017b248665d/output-manual-chain-cold.json`）；020默认隐藏内部代理并保持当前角色指针，16,026点实际UI求解/应用9项通过（`A/jobs/c3259ac35885493b90c65a9fb75dac83/output-display-ui.json`）。已保存服装/刚性附件也完成无制作插件独立冷开8项通过（`A/jobs/0b44df38c08d4afea39d91f793edd2f2/output-parts-cold.json`）。下表较早“完整点击链未验”仅为018历史范围，不再作为这三项当前流程阻塞；最终美术与包验收仍分别判断。

021–023原生平滑增量：人形独立`native Corrective Smooth`候选接口与GUI已接入；`modular_surface.create_candidate(source, rig, *, name, pin_boundary)`要求显式bool，人工入口`surface_pin_boundary`默认True，只接纳`HUMAN`。16,026点partial夹具核心9项通过，UI操作符11项通过；真实722,809点C017绑定皮肤经产品UI创建，neutral误差`9.07e-8 m`，left_hip45/shoulder60最大修正`2.01/2.93 mm`，关闭命名修改器后3姿态逐点回到原蒙皮，89 drivers与输入未变（`A/jobs/079f061ea21a4e7686fca5e1f0add2ea/output-native-surface-core.json`、`A/jobs/31a1941fae5841a9acb05cb86ef0cb99/output-native-surface-core.json`、`A/jobs/57c7cc0162e44c5298a1ba4d650a768c/output-human-surface-example.json`）。它只平滑微折，默认对照p99仅改善0.7%–2.7%，不修原权重或宏观肩胯；pin分支虽把新增接缝间隙归零，仍有髋部高尾变差（squat max +0.853、`>2` 5→19）。犬同配方`pin_boundary=True`的shoulder60高尾max `3.391→5.245`，产品入口拒绝四足，未继续扫参。数值改善不等于完整视觉签字。

**当前结论**：生成、真实Tripo人形/犬直绑、模板与部分组合冷开、真实犬四IK、人形547帧Mixamo、CMU 344帧/MMD 1441帧两条具名全片以及人工/AI同核入口已有具名实证。原局部helper约束消除了测试中的域外权重影响；可选原生修正平滑进一步减轻细碎硬折，固定开放边界避免把原接缝拉开。它不是主权重重算，宏观肩窝凹陷和大角度挤压仍是限制，不能称任意姿态美术通过。新自动高髋与旧人工低髋的具名比较已完成，未改善肩胯质量，未接纳作修复；该历史高模证据不代签新云低模。无收益的donor swap、ARMATURE平均机制、双调和及screened场已停止、未集成。下面018表保留当时范围，较新的长来源和表面修正实证见本轮验收，不能跨输入或跨快照混签。

| 路径/能力 | 已实现与代表证据 | 边界与未验 |
|---|---|---|
| 独立模板与模块组合 | 四份独立原生模板（人形/四足 × 基础/尾巴辅助）实际生成并在新进程核对原生控制、参考编辑、root 驱动和冷开，28 项检查通过；两组 partial 组合 `body/head/forelimb.L/hindlimb.L/tail` 完成 16,026/14,692 点体素绑定、6 个原生姿态/Action 和独立 CLI 冷开，64 个冷开见证一致。 | 组合是代表性技术夹具，不是全部模块排列；未含右肢/correctives 组合；不签 Tripo 美术或任意角色。四模板生成不等于最终安装包。 |
| 真实 Tripo 人形直绑/持久化 | 017 新 job 全部 722,809 生产行、29 组；原几何/UV 保持，权重 SHA 与 016 相同，保存/重开通过。 | 视觉未批准；肩腋 p99 未改善、髋补丁主要受 primary 权重影响，跨侧保守层/pelvis donor 实验未达到改善且未集成。018 未重跑该核。 |
| 真实 Tripo 犬直绑/投影/四 IK | 017 job 覆盖 716,030 点/1,401,858 面，32 主骨端点误差约 6.01e-8 米，原几何/UV 不变；生产投影全 716,030 行，11,261 采样 proxy/11,284 原 proxy，最大距离 0.0304593 < 0.0309353，barycentric 和误差 2.22e-16，并有独立成功 receipt；018 新进程冷开 106 drivers、四原生 IK、7 项检查通过。 | 仍有限大角度挤压等已知限制；不把四姿态/短 IK 当所有姿态自然。视觉未批准。 |
| 体素代理、完整几何与有界回写 | 保留完整计算代理体积；仅三个顶点都 supported 的完整三角作为 donor；没有支撑的代理点只从 donor 排除，不删除代理体积或生产点。全部生产行须在明确距离内回写；超距/无效/覆盖不完整拒绝，不用最近骨兜底。默认 26 邻接、回写 0.5–4 格（默认 3），真实人形 017 明确选用 3.5 格。 | 分件身体用完整身体计算域，但逐件独立求解、独立候选，**不是原子批量或同成同败事务**；任一件失败不自动撤回已完成候选。近接/重叠、任意坏网格和近接来源归属仍未普遍证明。 |
| 原生控制与 IK | 生成 rig 保留原生控制、约束、driver 和可编辑 Action；真实犬四 IK 端点误差约 1.64e-6–9.50e-6 米，皮肤随姿态变化；partial 组合的 6 姿态及冷开通过。 | 不承诺全 ROM、任意动作或视觉自然；辅助骨/肩胯美术仍另判。 |
| 可选原生姿态修正平滑（仅人形） | 独立Mesh副本，修改器顺序为Armature→`CORRECTIVE_SMOOTH`→原Subsurf；固定factor 0.5、5次、SIMPLE、中立BIND，`pin_boundary`显式bool、人工默认True；原Mesh/rig/权重/topology/UV/89 drivers不重算。021核心9项、022 UI操作符11项、023真实722,809点三姿态关闭恢复/输入保护通过：neutral `9.07e-8 m`、left_hip45 `2.01 mm`、shoulder60 `2.93 mm`；关闭修改器三姿态逐点回原蒙皮，输入未变。 | 只平滑微折，p99改善有限，不修原权重、宏观肩胯或穿透；pin把新增接缝间隙归零，但髋部高尾变差，微三角normal flip仍在。犬同配方高尾变差被产品入口拒绝，四足保留原蒙皮。数值/关闭恢复不等于视觉批准；不是BindingJob，无体素成功回执。 |
| 人形动作重定向 | 真实 722,809 点 Tripo 皮肤上完成 547 帧 Mixamo：121 fcurve/66,187 keys、547 时间点，source anatomical frame、逐帧 reset 和 4 个既有 native roll tweak 已集成，direction/joint 0°，upper_arm/thigh roll 为 0°；保存独立重开与 89 drivers 保持。另完成CMU `02_01_Rig` 344帧与MMD dance 1441帧两条具名全片：同一绑定皮肤、保存/独立冷开，direction 0°，CMU full rotation/twist 6.484°、MMD spine 42.845°未解决；native roll CMU 36.74°/MMD 82.44°→0°；各7代表帧×3视角有限皮肤近景/contact sheet。公开 UI 的 MIXAMO/PROFILE 各 5 帧、preview/stop/旧 Action/driver 保护 13 项通过，CMU 5→6 帧 8 项通过。 | spine 全片仍有 32.28°/spine1 26.35° 透明残余；CMU 6.484°、MMD 42.845°亦未解决。仅这两条具名CMU/MMD全片，其他长片、四足重定向、面部/morph/物理、连续接触/足底滑动、全 GUI 人工链未验。短 UI 入口不等于完整来源长片，有限皮肤近景不等于视觉/权重通过。 |
| Profile、MMD 与泛型文件 | `modular_retarget.convert(..., profile=...)` 与 `mapping=` 互斥入口可用；REST/CLIP_START/LEG_LENGTH 校验，REST_HIP/REST_HIP_HEIGHT 明确拒绝。真实 Mixamo profile、CMU exact BVH、MMD `MS_Uka_arm` 均完成 bounded 5 源帧桥接；MMD 使用实际 evaluated IK 证据，不是 copy 曲线。新增CMU `02_01` 344帧与MMD dance 1441帧两条具名全片已保存并冷开。 | bounded 5帧桥接仍只是历史短段证据；新增只覆盖上述两条具名全片。其它MMD/CMU长片、MMD面部/morph/物理、FBX importer端到端、generic non-CMU BVH独立素材仍未验。不得把短smoke或两条具名片段扩成全部家族。 |
| 人工 GUI、旧对象与交接 | 人工工作台调用同包核心；旧角色、旧 Action、旧绑定保留。新参考/新 rig 只清空工作台旧结果指针，不删除真实旧对象。若旧显示遮挡新骨架，应在视口隐藏旧骨架/旧候选显示，并把原始模型保持在中立静止状态显示，检查后再恢复；不要删除旧对象或覆盖工程来代替隐藏。 | 019可见UI整链已完成并保存；肩机制实验与更广人工接手仍未验收，本表不代签肩机制通过或完整人工接手。 |
| 安装与最终交付 | 018 隔离安装 A/B 的产品领域检查通过：真实 install/enable、三 operator、neutral、disable/enable、独立冷开/POSE。 | candidate018 的 `SOURCE-MAP`/`runtime-manifest` 仍是旧集合（13 个现有 modular/retarget 文件未声明），不能作为最终包；最终源码冻结后必须先运行 `finalize_package.py` 重建元数据并重新打 ZIP/seal。021–023仍是审计候选，不是最终安装/发布；最终安装/metadata未验，本目录未作为最终包安装/发布，领域 smoke 不等于最终包通过。 |

**证据入口**：模板冷开 `A/jobs/d72f9c5433a0461189719b4697e8f407/output-template-cold.json` 及 `delivery-candidate-018/templates/TEMPLATES.json`；partial 组合 `W/module-combinations-shard/REPORT.md`、`SUMMARY.json`；犬独立冷开 `A/jobs/59cc1a75f42a4aa6813b164928436286/output-dog-cold-reopen.json`；人形 547 帧与 bounded profile `W/retarget-shard/REPORT.md` §10–12 及 `evidence/bound-skin/`、`evidence/profile-bridge/`；CMU/MMD 全片 `W/retarget-shard/FULL-SOURCES-019.md`、`W/retarget-shard/evidence/full-sources-019/`；原生平滑 `A/jobs/079f061ea21a4e7686fca5e1f0add2ea/output-native-surface-core.json`、`A/jobs/31a1941fae5841a9acb05cb86ef0cb99/output-native-surface-core.json`、`A/jobs/57c7cc0162e44c5298a1ba4d650a768c/output-human-surface-example.json` 及 `W/native-surface-validation-shard/REPORT.md`；公开 UI `A/jobs/718ec7a2fabe4f63820d3c7ead3b84f5/output-motion-ui.json`、`A/jobs/92072a966e8d4192ace8b3c7bbe71e6c/output-motion-ui.json`；犬 017 绑定/投影 `A/jobs/b2d94adaa808411e8f05d94b205f273f/output-binding-report.json`、`A/evidence/canine-017-production-projection.json`；018 隔离安装领域 `A/jobs/3fca6c36f87748689bb9398a4d3d7324/output-install-report.json`。`W`为`<PROJECT_ROOT>/work/rigging-modular-20261112-v001`，`A`为`work/audit/<batch>/`。精确验证仍以各自报告的限制与原始 job 为准。

本次稳定性与工作台整理沿用已有能力。下表重定向素材结论来自先前具名验证；本次没有把有限样本扩成任意角色保证。统一入口为“角色工作台”，按阶段切换，完整参数在“进阶”，日志和审查在“开发”。

本次新增实证：完整角色入口在真实53,598点/15骨上完成体积求解、取消后拒绝提交、重新求解及应用、保存和独立重开；主C94、旧P1面部、第二比例、普通FK、Profile与MMD按各自路径核对原生数据保护。主C94胸臀/侧发/裙片完成捕获、预览、停止、烘焙及无制作包独立重开。详细作业、通过项与仍未验范围见本批验收报告；这些技术检查不签收连续无穿透、任意动作或素材美术质量。

旧相机Profile的弱签名不静默补签；提供显式迁移到新sidecar，保留原文件，并要求原文件SHA核对。迁移验证过的原生数据身份与真实camera VMD视觉对照分别说明。

核验环境：Windows / Blender 5.2.1 LTS，公共CLI 0.85.0。预设是具名有限支持，不等于任意同类素材保证。视觉采用骨骼动作自然、连贯与意图标准；下表沿用的旧重定向样本未精修蒙皮权重。

| 路径 | 已实现与代表证据 | 边界 |
|---|---|---|
| Mixamo FBX/场景 → C94原控制器 | 真实Samba完整438目标样本；身体、逐节手指、根运动；实际视口及头颈局部/全时域骨骼观察 | 原限位保留，复杂局部姿态仍须复核；不追逐源每骨精确重合 |
| CMU BVH预设 → C94 | 行走、跑动、跳跃；跑跳全部134真实key主/独审，原生骨视图与视频 | CMU样本无手指捕获；首帧切入变化保留明报，不自动裁掉或补过渡 |
| 通用FBX/BVH文件 → 显式Profile → C94 | 真实CMU改名BVH及其纯骨架FBX派生夹具，公共导入/导出Profile/校验/候选/正式ACTION/保存/独立cold/撤回 | 这是文件语义与时基技术夹具，非新增动作库；单来源骨架、显式mapping/轴/单位/fps；不保证任意文件 |
| 场景已有Action/Slot / Profile | 两个真实不同Action、人工多Slot机制、隔离恢复与复用校准已验；纯JSON配置 | 只采选定Action/Slot，源NLA最终合成尚未实现；Profile目标目前为C94 |
| 普通FK目标 | 真实CMU → Samba67骨；实际原生key编辑、保存重开、再次编辑，原骨长/rest/旧动作保护 | 身体FK回退要明确确认；不转换手指/表情/接触。原FBX骨尾方向与真实child-head存在rest偏离，非连接显示会呈断线；须结合实际关节点关系复核，不能为消图示空隙改rest |
| 第二真实比例C94目标 | 已有BJ短Samba30真实key、140骨全序列及侧视有限主审 | 不以改名当新比例，不推任意角色全片一键通过 |
| MMD身体/手指/根/IK | 真实Uka/自有PMX+VMD、原生IK台阶与独立cold；同clip组合候选持久使用 | 未知动态IK拒绝，未知静态通道明报；头发裙摆使用目标动态，非还原源物理 |
| MMD表情/口型/眼控 | dance同clip身体+face+gaze的ACTION保存/cold消费/撤回；真实laugh动态身体/表情；独立眼动机制fixture | dance表情/源眼向静态、laugh源眼向静态；眼控骨与眼球皮肤分开；缺眉/腮红等目标明报，不新造表情 |
| 可选MMD镜头 | 显式追加、设置活动/恢复、保存重开及人工轨道机制验证 | 未验真实camera VMD及原MMD播放器逐帧画面对照；精确后端身份见MMD-BACKEND-IDENTITY.json |
| 根运动 | 保留移动/转向、原地与按比例位移；各来源明确腿长等校准策略 | 不把不同适配器公式宣传为完全相同；不自动修曲线 |
| 足部检测/局部修正 | 原完整表面夹具与三骨锚点模式；真实跑跳只读、jump四帧独立预览/冷开，取消/失效/原态保护 | 显式标记/plane/帧秒校准；骨标记不是皮肤或完整sole。有限采样不证明连续接触；独立预览不等于用户批准生产apply |
| 手/身体接触提醒 | 具名人工观察Text、两骨/帧域/来源、身份及曲线/rest失效、保存重开重新校验 | 只提示人工复核，未自动碰撞识别，不自动修正，不授足部问题/批准权；自动检测报告仍为not_analyzed |
| 后台与规模 | 一次20个不同真实短窗口全部成功/收集；真实60秒MMD1441样本；小批坏项隔离、取消保成功及旧原生GUI交互证据 | 新20组为CLI，未作同组GUI响应观察；同步快照/收集会占用宿主时间，耗时见指南；64是上限非满载实测 |
| 正式使用/后续编辑 | 独立Action主输出、可选NLA；原绑定/Slot/轨道/动态开关保护与撤回；C94/FK key编辑cold | 稀疏/未知记录拒正式使用，接触候选走逐问题确认；文件不恢复RAM批准或复核勾选 |

旧网格、手部权重及服装缺陷保留历史，不作为本骨骼轮的阻塞，也未改签为皮肤通过。当前包不承诺任意Rigify/VRM/UE控制图、双人接触、复杂地形、源NLA合成、完整源物理或全球安装兼容。

本机证据：v015产品集成、v016跑跳、v017组合持久化及v018通用文件/规模/编辑性/人工提示/骨锚点。详细作业与精确产物由项目v018审计入口关联；该表不以测试数量替代视觉判断。
