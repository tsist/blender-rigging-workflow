# 模块化骨架：AI与公共执行入口

## 0.42.2统一候选说明（2026-11-13）

统一0.42.2候选同时含041中立采样与045 candidate002选段修复，API签名和原guards保持。modular_neutral.capture保存capture_metric=native_bbone_selected_point_samples_v1、capture_point_count和capture_sample_errors_meters；conservative_joint_envelope_max_meters及完整joint baseline仍保存，check_current逐字未改，容差仍1e-5m。不要用sample max替代当前姿态检查。

source_profile/profile_bridge描述既有profile及本次运行校准；selected_clip.range保完整Action域，selected_range保调用选段，source_origin_frame/world为实际选段原点。调用者profile JSON保持。人工片段关闭分支只验证None/None接线，不称全片off或桌面点击已测；完整取向残差与肩胯限制见本批报告。



## 本批低模与局部检查约定

本批Tripo样本采用Tripo云端重拓扑低模；原高模保留作来源和外形对照，既有高模实证保持各自范围，不代签低模。生产检查先继承已保存的普通蒙皮权重，明确记录TRANSFER；不在本地重拓扑，不反复全身重算。

肩胯问题须在相同视角和0–1色阶下并排比较相关各变形骨及辅助骨的权重，结合相关组总和、选点权重表与骨架视图，沿控制输入→ORG/MCH→最终变形骨/helper定位。按实际use_deform和消费关系判断骨职责，不能只看名称、最终几何或旋转角度幅值。先固定一层原因，再对明确点域/组做局部修复并复验；保留原候选和回退。

多Object身体的代理回写增加几何来源过滤：只保留当前来源三个顶点均归属且有体素支持的完整三角；任何生产点无完整有界覆盖时拒绝整个候选，不自动扩大距离。该方法是顶点几何近似，不能消歧完全同位置重叠，也不证明三角内部语义归属。


当前候选版本0.41.0；最终安装、交付索引与最新实证见本批审计报告。下面017/018等数字是历史验证快照。

本文件描述当前开发候选中实际存在的Python接口。人工工作台`modular_ui`调用这些同包核心；C94保留其原接口与严格身份校验。新模板由Blender已有Rigify组件构建，不要求私有C94模板或图谱。

以下代码必须在已核对身份的Blender场景或明确磁盘工作副本中运行，不能直接用普通系统Python操作`bpy`。公共CLI使用当前`extension.run`契约、精确包ZIP及资源SHA；不要用历史`rigging-workflow`定位器的成功冒充本包执行。包装脚本、依赖与信任声明按当前CLI Schema准备，本文不另造专用命令。

## 状态与示例约定

- 本文列出的函数、字段和返回对象来自当前源码；代码片段展示用法，不代表全部输入已运行通过。
- 真实人形722,809点与犬716,030点均有完整生产行及原几何／UV保护证据，之后补齐模板/部分组合、原生控制、可见人工链与独立冷开。主权重和局部helper与表面后处理分开报告：可选修正平滑能减轻细碎硬折，不能宣称权重串扰或所有宏观挤压已解决。人形动作另有两条具名全片（CMU 344帧、MMD 1441采样）在真实绑定皮肤上保存/冷开；它们不覆盖其它长片或家族。旧015四姿态不混签017全验，自动/人工骨位与具名来源范围分别核对；新自动高髋与旧人工低髋的比较已完成且未接纳作修复；历史高模证据不代签云低模。
- 所有`<LOCAL_PATH>`均为用户自己的**绝对路径占位**，对象名也须替换成当前场景真实名称。本文不携带账号、私有模型、本机任务或凭据路径。
- 使用已安装的精确同版包；不要在同一进程混入其他目录的同名`rigging_workflow`。

```python
from pathlib import Path
import bpy
import rigging_workflow

work_file = Path(r"<LOCAL_PATH>")
jobs_root = Path(r"<LOCAL_PATH>")
if bpy.context.mode != 'OBJECT':
    raise ValueError('请先明确结束当前编辑，再开始制作')
if not bpy.data.filepath or Path(bpy.data.filepath).resolve() != work_file.resolve():
    raise ValueError('当前工程与本次明确工作副本不一致')
scene = bpy.context.scene
source = scene.objects['你的原始身体Mesh']
```

脚本不自动打开或覆盖别的工程。需要保存时使用用户明确的新输出路径，保留原件和依赖。

## 1. 参考与模块配置

实际接口：

```text
modular_rig.prepare_reference(scene, *, kind='HUMAN', label='基础人形',
    configuration=None, allow_runtime_enable=False) -> reference Object
modular_rig.compose_reference(scene, reference, *, configuration,
    label=None, allow_runtime_enable=False) -> new reference Object
modular_rig.confirm_reference(reference, *, allow_runtime_enable=False) -> record dict
modular_rig.generate(scene, reference, *, label=None,
    allow_runtime_enable=False) -> dict，包含rig
modular_rig.read_reference(reference) -> record dict
modular_rig.read_generated(rig) -> record dict
```

`kind`是`HUMAN`或`QUADRUPED`。`enabled_modules`可选项为`body/head/forelimb.L/forelimb.R/hindlimb.L/hindlimb.R/tail/correctives`，其中body必需。当前配置只接受`segments=1`、`bbones=1`；不能传任意Rigify参数。

```python
from rigging_workflow import modular_rig

config = {
    'kind': 'HUMAN',
    'enabled_modules': [
        'body', 'head', 'forelimb.L', 'forelimb.R',
        'hindlimb.L', 'hindlimb.R', 'correctives',
    ],
    'segments': 1,
    'bbones': 1,
}
reference = modular_rig.prepare_reference(
    scene, kind='HUMAN', label='测试人形', configuration=config,
    allow_runtime_enable=True,
)
reference_name = reference.name
```

`allow_runtime_enable=True`明确允许在当前进程启用Blender已有Rigify，不下载或安装组件，也不调用保存用户偏好。四个涉及生成环境的公开函数默认均为False；如果当前进程尚未就绪，调用者须明确传True或先完成合法的会话启用。半注册状态会拒绝，不能手工补属性绕过。

四足使用`kind='QUADRUPED'`及同名配置kind。手掌／头部整体、无精细手指／面部是当前模板范围；`tail`和`correctives`只按明确组合启用。

## 2. 定位提议、镜像与人工确认

```text
modular_positioning.estimate(reference, target_objects, *,
    forward_axis='-Y', up_axis='Z') -> report dict
modular_positioning.require_current(reference) -> report dict
modular_positioning.mirror(reference, side='L') -> report dict
```

```python
from rigging_workflow import modular_positioning

position_report = modular_positioning.estimate(
    reference, [source], forward_axis='-Y', up_axis='Z',
)
```

这里停止，交给人工查看正面、侧面、斜视与各关节；estimate只写新参考的候选骨位，不调用确认、不替用户判断。接口接受明确Mesh列表用于读取联合表面；多个身体Mesh也可用下文`body_sources`逐件绑定，但不是统一批事务。

人工手调保持参考骨结构与命名；需要改变模块用compose，不直接增删骨。`mirror(reference, side='L')`是把L复制到R，`side='R'`反之，按参考局部X镜像位置及roll；需要先有estimate状态并通过来源检查，不能当成任意世界轴对称工具。

检查完成后再执行：

```python
# 如使用过estimate，先确认其源对象、几何与参考框架仍匹配。
modular_positioning.require_current(reference)
confirmed = modular_rig.confirm_reference(
    reference, allow_runtime_enable=True,
)
generated = modular_rig.generate(
    scene, reference, label='测试人形', allow_runtime_enable=True,
)
rig = generated['rig']
metadata = modular_rig.read_generated(rig)
```

完全手工的新参考没有estimate记录时，不需要调用`require_current`，但仍须真实检查后调用confirm。修改rest或生成设置会使旧确认过期。保存重开若来源记录过时，重新准备／提议，不修改签名放行。

面板支持`-Y/+Y/-X/+X`；已核对`-Y/+Y`，并对真实人形±X、犬±Y有定位分片代表证据（主包018未重跑定位）。犬类后膝、其他方向与未估计域仍须按同版报告和当前角色复核；上例使用-Y。目标姿态不清、肢体难以分离或某域未估计时，如实保留报告，不能用模板默认点冒充自动识别成功。

## 3. 单Mesh体素绑定与后台任务

```text
modular_workflow.prepare_proxy(source, *, mode='AUTO', cells_longest=96)
    -> (proxy Object, report dict)
modular_workflow.prepare_body_proxy(sources, *, mode='AUTO', cells_longest=96)
    -> (shared-body calculation proxy Object, report dict)
modular_workflow.start_binding(source, rig, *, proxy=None, proxy_mode='AUTO',
    settings=None, jobs_directory=<absolute path>, body_sources=None,
    transfer_distance_cells=3)
    -> live BindingJob
job.status() -> dict
job.cancel()
job.apply(*, name=<新候选名，必须非空且未使用>) -> new bound Mesh Object
```

```python
from rigging_workflow import modular_workflow

job = modular_workflow.start_binding(
    source, rig, proxy_mode='AUTO',
    settings={
        'cells_longest': 96,
        'max_cells': 2_000_000,
        'max_memory_mb': 2048,
        'max_seconds': 1200,
        'neighbor_count': 26,
        'require_all_bones_seeded': True,
    },
    jobs_directory=jobs_root, transfer_distance_cells=3,
)
```

这些值是当前入口的默认规模示例，不是验收秒数或质量保证。新入口默认26邻接加权体积图，仍拒绝穿面邻接；合同也接受明确选择6邻接。`RAW`要求原始表面可定义闭体，`VOXEL`明确建立内部体素代理，`AUTO`检查后选择。原始生产拓扑、UV和外观保持；代理可能闭合窄缝，必须看实际姿态与轮廓。

当前求解保留完整代理体积，用体素场支撑检查明确列出可采样的代理表面点；仅三个顶点都supported的完整三角用于表面回写。unsupported代理点只从表面转移供体中排除，**不删除代理体积中的点或微小碎壳**。没有任何完整可用三角时拒绝建立转移场。

提交前通过共用`skinning.transfer_surface_field()`将代理场有界插值到全部原生产点，报告方法为`barycentric_volume_field_transfer`，再按需做局部helper分配。`transfer_distance_cells`是`start_binding()`的直接关键字，不属于settings；默认3，允许明确的有限数值0.5–4。真实人形017记录明确采用3.5，不是默认变化或自动扩距。距离由该值乘当前cell大小换算，报告另有米制距离。超距、无效权重或原生产行覆盖不完整时拒绝，不自动增大上限、不最近骨兜底。整体绑定方法仍区分`volume_proxy_surface_transfer`和`volume_proxy_surface_transfer_with_local_correctives`，不能把代理回写与服装供体转移混称成一次原始点直接扩散。

任务准备后保存支持域记录，实际回写时在候选提交前保存投影记录；尚未执行到相应阶段时不会有后阶段产物。以下证据用于复核，不恢复提交权限：

| 文件 | 含义 |
| --- | --- |
| `field-support.json` | supported／unsupported代理点计数、全部unsupported索引、可用转移三角数和采样索引SHA；绑定输入manifest、数组、source-state、代理几何及分配／距离策略的SHA，以及来源身份和明确回写距离。该文件在提交时受完整性检查。 |
| `input.json`的`proxy_sample_indices` | 实际代理场采样点在完整代理顶点中的索引顺序。 |
| `transfer-projection.npz` | 每个原始RAW生产行对应的`sample_vertex_ids`（受支持采样顺序的三角顶点ID）、`proxy_vertex_ids`（完整代理RAW三角顶点ID）、`sample_to_original_proxy_vertex`（采样点到完整代理的映射），以及`barycentric`、`distances_world_units`和`transaction_state`。报告记录文件SHA、行数、两种ID顺序及距离边界。 |

017明确区分两类索引：`proxy_vertex_ids == sample_to_original_proxy_vertex[sample_vertex_ids]`，不能再把`proxy_vertex_ids`解释成压缩采样序号。生产行顺序保持原RAW顺序；`transaction_state`为`projection_only_not_a_submission_receipt`，投影文件在候选提交前创建，其存在不等于成功绑定或恢复apply权限，最终成功另看正式apply回执与真实候选。

全部原始生产点仍须有界回写，不得通过丢弃生产行或删除碎片改变验收范围。这些路径仍为候选，不能据此保证任意碎片或不完美网格可用。

在后续有界状态检查或工作台定时回调中读取`job.status()`，不要在主线程无界循环等待。状态为`succeeded`且未取消后再显式应用；失败／取消不得应用部分权重。

```python
state = job.status()
if state.get('status') == 'succeeded':
    candidate = job.apply(name='你的新身体蒙皮候选')
elif state.get('status') in {'failed', 'cancelled'}:
    raise RuntimeError(str(state.get('error', state)))
# running时保留live job，由后续回调继续检查；不要重新提交同一任务。
```

该live对象持有提交权限，保存JSON或作业路径不能恢复它。源、rig、代理、帧或单位改变后旧结果会被拒绝，应重新准备。当前新绑定入口接无旧权重、Key、父级、约束和动画的本地中立Mesh，修改器仅接受Subsurf；已有角色局修另走权重维护。

启用辅助骨时，主骨先体积扩散并有界回写生产点，辅助骨再通过局部让渡获得权重，不作为额外全局源。不能只用求解成功或权重非空代替腋下／胯部及辅助骨的动作验收。

## 3A. 分件身体：完整计算域、逐件独立候选

`body_sources`需要1–64个不同的合法中立Mesh，必须包含当前`source`。只列完整身体生产源；服装、薄片和刚性附件另走第4节，不混入身体来源。不要把代理、已绑定候选或上一轮输出再次加入来源。

```python
body_collection = scene.collection.children['你的身体源分件集合']
body_sources = [obj for obj in body_collection.all_objects if obj.type == 'MESH']
part_source = scene.objects['本次要绑定的原始身体分件']
if part_source not in body_sources:
    raise ValueError('当前分件必须属于明确的完整身体来源')

# 自动定位同样使用这一份完整来源列表；人工检查后再确认与生成。
# modular_positioning.estimate(reference, body_sources, forward_axis='-Y', up_axis='Z')

part_job = modular_workflow.start_binding(
    part_source, rig,
    proxy_mode='AUTO', body_sources=body_sources,
    settings={'cells_longest': 96, 'neighbor_count': 26},
    jobs_directory=jobs_root, transfer_distance_cells=3,
)
```

此调用自动建立完整身体的内部几何域，但只返回当前分件的live任务；按普通`status/cancel/apply`流程创建该件独立候选。下一件另起任务。不是一次全体提交／回滚的batch事务，也不能把一件成功算作全体完成。

若需要显式准备计算代理，可使用实际接口：

```python
body_proxy, proxy_report = modular_workflow.prepare_body_proxy(
    body_sources, mode='AUTO', cells_longest=96,
)
explicit_proxy_job = modular_workflow.start_binding(
    part_source, rig, proxy=body_proxy,
    body_sources=body_sources,
    settings={'cells_longest': 96, 'neighbor_count': 26},
    jobs_directory=jobs_root, transfer_distance_cells=3,
)
```

两段是自动代理与显式代理的替代用法，不要对同一件同时重复提交。各任务冻结并核对全部`body_sources`及代理；任一来源改变要重新准备。重用一个代理对象不表示已复用求解缓存或拥有跨任务提交权。

人工对应字段为`modular_ui`的`body_collection`：“分件身体集合（可选）”。选择集合后定位读取其中全部Mesh，绑定仍逐件选择`source`。不要求合并生产Mesh或重建其UV；联合三角化只发生在私有计算输入。已有分件身体/partial组合的代表证据，但不是全部模块排列；每件仍是独立候选，不是原子批量。

## 4. 服装／薄片表面转移与刚性附件

实际共用接口为：

```text
skinning_policy.prepare_transfer(target, rig, donor, *, max_distance_meters,
    bone_names=None, donor_vertex_indices=None, local_vertex_indices=None,
    alpha=None, editable_bone_names=None) -> live PreparedWeights
skinning_policy.prepare_rigid(target, rig, assignments) -> live PreparedWeights
plan.apply(*, name=<新候选名，必须非空且未使用>) -> new Mesh Object
plan.cancel()
```

全新服装／薄片目标必须符合无旧绑定输入要求。donor是同一rig上实际已经应用权重的中立身体候选；不是未应用的数组、计算代理或另一个角色。

```python
from rigging_workflow import skinning_policy

garment = scene.objects['你的未绑定服装或薄片Mesh']
body_candidate = scene.objects['你已检查的身体蒙皮候选']
transfer_plan = skinning_policy.prepare_transfer(
    garment, rig, body_candidate,
    max_distance_meters=0.03,  # 示例；由本模型实际间距明确决定。
)
garment_candidate = transfer_plan.apply(name='你的服装转移候选')
```

默认读取供体全部实际可用最终deform列，包含已应用的辅助骨权重；如果显式给`bone_names`，不能静默丢弃供体选域内仍有正权重的实际形变列。可用`donor_vertex_indices`限定完整供体三角范围。转移结果方法是`applied_raw_surface_transfer`，不能标成体素求解。

```python
attachment = scene.objects['你的未绑定刚性附件Mesh']
deform_name = '你明确选定的最终DEF骨名'
if deform_name not in metadata['deforms']:
    raise ValueError('附件必须明确使用本角色最终形变骨，不能选控制或机制骨')
rigid_plan = skinning_policy.prepare_rigid(
    attachment, rig,
    [{'bone_name': deform_name,
      'vertex_indices': list(range(len(attachment.data.vertices)))}],
)
attachment_candidate = rigid_plan.apply(name='你的刚性附件候选')
```

`assignments`的多行可表示不同骨的明确顶点域，但必须覆盖全部目标RAW顶点且不能重叠。已有绑定的局部维护才使用`local_vertex_indices/alpha/editable_bone_names`；它保护锁组与未声明组，不应和全新绑定混用。

对应人工入口：“绑定与蒙皮→进阶→05·权重策略”，目标／rig／供体和实际方法与以上核心一致。

## 5. 原生控制与人形重定向候选

生成rig保持原生控骨、约束、driver和可编辑Action。控制／机制／形变骨分别记录；蒙皮只接受`read_generated(rig)['deforms']`中的最终骨，不能把任意名字的骨当成可写输出。IK/FK原生属性0=IK、1=FK；直接改属性和保姿态匹配不是同一操作。

020起`prepare_proxy()`/`prepare_body_proxy()`返回的内部代理在当前View Layer默认隐藏（`hide_set(True)`），保留RAW几何、报告和对象；无需解除隐藏即可求解。`BindingJob.apply()`的数值与提交规则未因此变化。人工入口创建候选后仍选中Mesh，工作台角色指针保留为生成rig。

### 018 UI入口与公共签名

018起，`modular_ui`的`motion_source_kind`提供`MIXAMO/CMU/PROFILE`三条来源路径，并调用同包`modular_retarget.convert(..., profile=profile, mapping=mapping, ...)`：MIXAMO使用可选骨名映射；CMU用精确预设；PROFILE读取通用来源配置，或读取MMD导入配置并按`source_clip_id`选择片段。公开UI短入口证据为：MIXAMO/PROFILE各5源帧、13项通过（preview/stop、旧Action/drivers/source保护），CMU 5源帧→6输出帧、8项通过。短入口不等于完整长片或所有来源。

当前真实签名：

```text
modular_retarget.convert(source, target, profile=None, *, mapping=None, clip=None,
    source_fps=None, start=None, end=None, output_start=1, motion='PRESERVE',
    sample_step=1, action_name=None, progress=None, cancelled=None,
    adapter_factory=None, check_files=True) -> dict
modular_retarget.convert_profile(source, target, profile, **kwargs) -> dict
retarget_action.begin_preview(target, action, *, slot=None) -> dict
retarget_action.stop_preview(target, *, notify=True) -> dict
retarget_action.has_preview(target) -> bool
```

```python
from rigging_workflow import modular_retarget, retarget_action

motion_source = scene.objects['你的来源动作骨架']
motion_result = modular_retarget.convert(
    motion_source, rig,
    mapping=None, source_fps=30,
    output_start=1, motion='PRESERVE', sample_step=1,
    action_name='你的人形动作候选',
)
action_candidate = motion_result['action']
preview_report = retarget_action.begin_preview(
    rig, action_candidate, slot=motion_result['slot'],
)
# 由用户检查实际播放；结束临时预览时另行调用：
# retarget_action.stop_preview(rig)
```

`profile`与`mapping`互斥；二者同时给出会明确报错。`profile=None`保留原`mapping/source_fps`路径；显式`profile`走Profile桥，支持REST、CLIP_START、LEG_LENGTH，明确拒绝REST_HIP、REST_HIP_HEIGHT和其它比例模式。`mapping=None`只尝试已实现的来源名预设并检查实际层级，不是任意骨名自动识别。自定义mapping为语义→真实源骨名的纯数据dict；基础必需语义是hips/chest/head及左右upper_arm/forearm/hand/thigh/shin/foot，可用语义以`retarget_source.mapping()`实际校验为准。该入口只转换身体，`include_fingers=False`。

真实人形547帧Mixamo已在722,809点绑定皮肤上完成：121 fcurve/66,187 keys、direction/joint 0°，并集成source anatomical frame、逐帧reset和4个既有native roll tweak；upper_arm/thigh roll为0°，spine/spine1仍有约32.28°/26.35°透明残余。真实Mixamo profile、CMU exact BVH、MMD `MS_Uka_arm`均完成bounded 5源帧桥接（direction/joint 0°，rotation分别约11.9204°/4.8110°/10.7793°）；MMD使用实际evaluated IK证据，不是copy曲线。bounded桥接仍是历史短段证据。

### CMU 344 / MMD 1441 具名全片

另两条现有完整片段走同一`modular_retarget.convert(..., profile=...)`入口，在722,809点绑定皮肤`VSH_Tripo_Human_Bound_C017`上完成并保存、独立冷开：

| 片段 | 范围与时基 | Action / 残余 | 持久化与皮肤观察 |
|---|---|---|---|
| CMU `02_01_Rig` | 源`1..344 @24`→输出`1..344 @24`，全344采样 | 121 fcurve/41,624 keys；direction/joint 0°；完整rotation/twist max 6.484163°/6.484283°（spine）；独立非退化joint 0.035066°；native upper_arm/thigh roll 36.741572°→0° | 目标196骨/29 DEF/70 controls/89 drivers、722,809点皮肤保持；7代表帧×3视角共21张实际皮肤近景与contact sheet；输入/输出SHA前后一致 |
| MMD `MS_Uka_arm` dance | 源`0..1800 @30`（含端点）→输出`1..1441 @24`，1441采样 | 121 fcurve/174,361 keys；direction 0°；spine完整rotation/twist max 42.844950°/42.844967°，未解决；独立joint 0.035813°；native roll 82.443433°→0° | 目标196骨/89 drivers保持；8个源Action曲线/key/范围保留，facial/display仅保存不驱动；7代表帧×3视角共21张近景/contact sheet |

CMU 产品报告中的`joint_direction_residual_max=89.240192°`是源chest参考退化（`Spine1.head==Neck.head`，源向量`1.164e-10 m`）造成的无效角度，不是目标折断；独立非退化joint为0.035066°，原字段未改、门未放宽。CMU源`1→2`的`hand.R`相邻旋转跳变`89.972941°`由源意图带入，生产选段应决定是否裁掉前置帧。MMD源腿部为真实evaluated IK求值，不是复制曲线；目标手臂外展/手掌体积与chibi源不同，肩肘膝胯布褶属于比例/权重/控制域限制，不归因于本次动作。两条的皮肤图只是7帧有限采样，不是连续接触、足底滑动或生产beauty验收。

除上述两条具名片段外，其它CMU/MMD长片、四足重定向、MMD面部/morph/gaze/物理、FBX importer端到端和generic non-CMU BVH仍未验；不能把bounded 5帧smoke或这两条片段扩成全部家族。两条全片的证据与限制见`retarget-shard/FULL-SOURCES-019.md` §0–8；更早的547 Mixamo与bounded短段历史见`retarget-shard/REPORT.md` §10–12。

目标必须是支持的新模块化人形；四足不在首版重定向范围。tail/correctives已取得合成短动作候选检查，生产动作和更广输入尚未验收，删减模块的适配也以同版报告为准。输出是独立Action及Slot，预览会暂时绑定并报告`production_published=False`，stop会恢复并保留候选；不能把它写成已经正式采用。正式接手、保存冷开及旧动作保护以最终交付流程和实证为准，本文不臆造新modular正式发布函数。

## 6. 模块重建、保存与恢复

### 可选人形表面候选

实际签名：

```text
modular_surface.create_candidate(source, rig, *, name, pin_boundary)
# 返回 {'candidate': Mesh Object, 'report': dict}
```

`name`必须是非空、尚未占用的候选名；`pin_boundary`没有Python默认值，必须显式传`True/False`，不能省略或从其它资产推断。只接纳`configuration.kind='HUMAN'`；四足实测为混合结果，产品入口明确拒绝该配方。从保留的普通中立蒙皮建立独立原生后处理副本：

```python
from rigging_workflow import modular_surface
surface_result = modular_surface.create_candidate(
    body_candidate, rig, name='你的独立平滑候选', pin_boundary=True,
)
surface_candidate = surface_result['candidate']
```

固定`factor=0.5`、`iterations=5`、`scale=1`、`SIMPLE`、`rest_source='BIND'`、无顶点组遮罩；输出Armature→`CORRECTIVE_SMOOTH`→原Subsurf，中立外观误差须≤1e-5m。源Mesh/rig/权重/topology/UV及89 drivers不重算，候选是独立Mesh副本；已有`rigging_modular_surface_v1`的候选不能再次叠加。返回的report为`rigging.modular.surface.v1`，含`candidate/modifier/source/rig/pin_boundary/bound/neutral_error_meters/source_state_sha256/raw_geometry_preserved/source_unchanged/weight_calculation_changed/artistic_approval=None/recovery`；`artistic_approval=None`不是通过标志。

人工入口在同一面板“角色工作台→简洁身体控制→人形可选：原生姿态修正平滑”：`surface_pin_boundary`（`BoolProperty`，默认True，名称“固定开放边界”）、`surface_candidate`指针和操作符`rigging_modular.create_surface_candidate`。UI调用同核`create_candidate`；只有当前有蒙皮候选且`kind='HUMAN'`时显示。核心要求显式bool，UI默认True只属于人工入口，不是Python默认值。

已跑证据：16,026点partial夹具核心9项、UI操作符11项通过（native bound、RAW geometry、关闭修改器逐点一致、非中立拒绝、绑定后失败回滚、源/rig保护、UI指针与配方）；真实722,809点C017皮肤经产品UI创建，neutral最大修正`9.07e-8 m`，left_hip45/shoulder60最大修正`2.01/2.93 mm`，关闭修改器三姿态逐点回到原蒙皮，输入文件与89 drivers未变。它只平滑微折，默认对照p99仅改善0.7%–2.7%，不修复原权重串扰、宏观肩胯凹陷或穿透；pin分支把新增接缝间隙归零，但髋部高尾变差，微三角normal flip仍在。犬同配方`pin=True`的shoulder60高尾max `3.391→5.245`，产品入口拒绝四足；未继续扫mask/factor/iterations。这些数值与关闭恢复不构成完整视觉签字。

恢复：关闭候选上命名的“原生姿态修正平滑”修改器，即回到保留原权重的求值；也可直接使用原蒙皮对象。不要应用该修改器、删除原蒙皮或把平滑候选送回仅接普通绑定的入口；后续权重转移仍用原普通蒙皮。它不是BindingJob、不生成体素成功回执，也不宣称权重串扰本身已修复。

需要改变模块（例如追加尾巴）时，从保留参考建立新参考候选，再确认、生成并重新绑定／转移权重：

```python
next_config = dict(config)
next_config['enabled_modules'] = [*config['enabled_modules'], 'tail']
new_reference = modular_rig.compose_reference(
    scene, reference, configuration=next_config,
    label='你的尾巴版本', allow_runtime_enable=True,
)
```

这是新参考候选：共用骨保留源参考当前编辑，原参考与旧rig不改。重新检查后确认并生成新rig，再重新绑定或做明确的权重转移及动作重定向。人工工作台创建新参考／新rig时清空旧结果与动作指针，但保留旧对象和Action；它们不会因新生成自动迁移到新结构。AI调用者也应明确维护自己的新旧引用，不把旧变量误当作新rig的结果。

在新的、用户明确的工程路径保存选定候选和依赖。重开后核对真实rig身份、Mesh的Armature目标、Action/Slot与代表姿态。后台live job、PreparedWeights计划和临时预览权限不会由文件路径恢复；源或设置改变时重新准备，不改旧SHA、身份记录或neutral证据放行。

## 当前验证边界

分件身体、26邻接图和保完整代理体积的有界回写均为当前候选接口。两真实模型均有017技术绑定与持久投影证据，018补充四模板/两组partial组合/独立犬冷开/公开UI短入口，019–020补充可见UI整链与显示修正，021–023补充仅人形的原生平滑候选及真实皮肤三姿态检查。CMU 344帧、MMD 1441帧两条具名全片已有保存/冷开和有限皮肤近景，但不覆盖其它长片或家族；视觉肩腋／髋前未通过，最终安装/metadata未验。新自动高髋与旧人工低髋的比较已完成且未接纳作修复；历史高模证据不代签云低模。旧犬015四姿态不代签017全流程。不能把接口存在、单项数值通过或短动作通过写成全部生产角色成功。`start_binding()`的第一个参数仍是单个当前生产Mesh，多来源通过`body_sources`明确传入；每件独立求解、独立候选，没有承诺全体同成同败的batch事务。


### 0.42.1 片段起点一致性（隔离候选）

`convert(..., profile=..., start=58, end=82)`的CLIP_START指本次选段起点58；仍用REST朝向和LEG_LENGTH比例，不把第58帧当REST。共享核接管来源Action/Slot/REPLACE/noNLA后原生采本次起点，原frame/hip身份门保留。调用者profile、源Action与原导入时基不写回。Action的selected_clip.range保完整来源Action范围，selected_range保所选范围，source_origin_frame/world与本次实测一致；profile_bridge.clip同时标注action_range/profile_range/selected_range。

人工“生成独立人形动作候选”新增默认关闭的“使用动作片段”，开启后填写来源起止帧；与AI调用同核，关闭仍使用原完整范围。片段选择不承诺消除来源跳变、蒙皮/接触或完整取向残余。本地0.42.1候选未替换日常安装或完成主WM验收。
