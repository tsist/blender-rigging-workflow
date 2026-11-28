# 局部权重修复套组：AI / Python 调用指南

## 0.42.2统一候选说明（2026-11-13）

0.42.2统一候选沿用本页0.42.0区域调用与数值契约；本次没有新增solver API。048实验分析没有可采用权重，不打入产品。精确ZIP/当前来源与回退以包外PACKAGE.json为准。


对应 **0.42.0 本地候选版**，尚未替换日常安装；已核 API 文件为本批工作树的 `weight_repair.py`、`weight_repair_ui.py` 和 `weight_repair_diagnostics.py`。例子是有明确输入前提的调用模板，本分片只核对签名及结构，没有运行这些示例。通过已加载插件的实际包导入；下面以 `rigging_workflow` 为包名，若安装器赋予其它命名空间，仅调整导入，不复制算法实现。Stage 在 UI 中称“阶段”；可见文案为“加入阶段（默认新建）”“阶段与操作顺序”“删除阶段”“当前计划”“高级配方编辑”，函数、属性和操作标识不变。

## 1. 真实公开签名

```text
mesh_fingerprint(target)
capture_region(target, *, indices=None, mask_group=None,
               rings=0, barriers=(), allowed_ids=None)
bind_recipe(recipe, targets)
save_recipe(recipe, path)
load_recipe(path)
prepare(targets, rig, recipe)
build_mirror_pairs(target, left_ids, right_ids, *, axis=0,
                   plane_meters=0.0, tolerance_meters, center_ids=())
confirm_pairing(target, pairing)
run_batch(items, *, continue_on_error=False, cancelled=None)
invalidate_scene_context()
release()
```

`prepare` 返回 `PreparedRepair`，提供 `.report`、`.preview(name_prefix='权重区域预览')`、`.apply(name_prefix='局部权重候选')` 和 `.cancel()`。preview/apply 返回 Object 列表。不存在公开 `make_region`、Pairing 对象的 `.confirm()` 或专用 rigging CLI 子命令；配对是 JSON dict。

输入必须是现有核认可的生成角色、中立校准、对象模式、本地独立 Mesh、单一同 rig Armature 栈及受支持可选 Subsurf。不得改 metadata 冒充身份、自动丢 Key/动作或自动归一输入。实际 deform 权重有限、每分量 0–1，逐点和在 1±1e-5。共享 Mesh 与已知非蒙皮 consumer 冲突拒绝。原源不改，提交只创建独立候选。

## 2. 多目标、多区域、有序 Stage

下面函数接收已核对的对象、RAW 点号和实际 deform 组名。`body_ids_a/body_ids_b` 必须是本 body 的非空合法整数列表、互不重叠；`part_ids` 属于 part。两网格均绑定同一个 rig，Mesh 数据独立；groups 是对应网格已有且非锁、无 consumer 冲突的 deform 列。不能拿示例数字代替实际解剖诊断。

```python
from rigging_workflow import weight_repair as wr

def prepare_two_targets(body, part, rig, body_ids_a, body_ids_b,
                        part_ids, body_groups, part_groups):
    ra = wr.capture_region(body, indices=body_ids_a, rings=0)
    rb = wr.capture_region(body, indices=body_ids_b, rings=0)
    rp = wr.capture_region(part, indices=part_ids, rings=0)
    recipe = {
        'schema': 'rigging.weight-repair.recipe.v1',
        'regions': {
            'body_a': dict(ra, target='body'),
            'body_b': dict(rb, target='body'),
            'part_a': dict(rp, target='part'),
        },
        'stages': [
            {'label': '独立区域同步平滑', 'operations': [
                {'region': 'body_a', 'kind': 'SMOOTH',
                 'groups': list(body_groups), 'iterations': 1},
                {'region': 'body_b', 'kind': 'SMOOTH',
                 'groups': list(body_groups), 'iterations': 1},
                {'region': 'part_a', 'kind': 'SMOOTH',
                 'groups': list(part_groups), 'iterations': 1},
            ]},
            {'label': '明确读取前阶段结果', 'operations': [
                {'region': 'body_a', 'kind': 'REBALANCE',
                 'groups': list(body_groups),
                 'factors': {name: 1.0 for name in body_groups}},
            ]},
        ],
    }
    targets = {'body': body, 'part': part}
    return wr.prepare(targets, rig, recipe), recipe, targets
```

第二阶段倍率 1.0 仅展示结构，不是有意义的修复方案。Smooth 需要可用邻接和正质量支持，不保证任意选点能通过。实际方案须由病灶证据决定，不自动扫参数。

调用函数取得 `plan, recipe, targets` 后，先读 `plan.report`；如需范围显示调用 `previews = plan.preview(name_prefix='检查范围')`。决定提交时调用 `candidates = plan.apply(name_prefix='修权候选')`；不提交则 `plan.cancel()`。这些是分开的用户/AI工作步骤，不能在示例封装中自动批准 report 或变形质量。不要在 prepare 与 apply 间改变源、rig、recipe、锁、帧、单位或 Scene。

同 Stage 所有操作读同一冻结场；同目标正 alpha 重叠点拒绝，即使 groups 不同。跨 Stage 顺序明确，每步 float64 算法→alpha 一次→float32 量化，后一阶段读取该结果。每步只保留本步 groups 的原质量 M，其它列不借质量；M=0/alpha=0 保持。含锁组拒绝整步。单 Plan 全目标候选原子，跨角色批次不是全批原子。

## 3. 区域与操作字段

Region 为 `ids/alpha/read_ids/barriers/binding` 等 JSON 内容，recipe 另加 `target` 逻辑 key。ID 必须严格整数，不接受 bool 或浮点截断。读域包含写域。`indices` 与 `mask_group` 二选一；两者均省略时读取当前可见 RAW 选点。Edit Mode 捕获先同步数据，但 prepare 必须回 Object Mode。

```python
# ids、blocked_edges、allowed 为调用前已核对的本对象 RAW 数据。
region = wr.capture_region(target, indices=ids, rings=2,
                           barriers=blocked_edges, allowed_ids=allowed)
# 或以全部正值 mask 点作为来源；rings=0 使用 mask 值作 alpha。
mask_region = wr.capture_region(target, mask_group=mask_name, rings=0)
```

扩圈种子 alpha=1，层 d 的 alpha=1−d/(rings+1)，读域含允许的外一圈。barriers 是真实 RAW 边，不是任意两点连线；不会自动识别解剖分界。

下列是 `stage['operations']` 中单个操作的结构。`g0/g1` 为已核对的不同实际组名，region_name 为配方区域；这些片段应选用并加入配方，不表示依次全部执行：

```python
# 完整或部分精确赋值：values.keys 与 remainder 互斥，合并等于 groups。
assign = {'region': region_name, 'kind': 'ASSIGN', 'groups': [g0, g1],
          'values': {g0: 0.2}, 'remainder': [g1]}
# 0.2 仅结构示例；每点若 M<0.2 或剩余组不能承担余量，则整步拒绝。
transfer = {'region': region_name, 'kind': 'TRANSFER_AMOUNT',
            'groups': [g0, g1], 'amount': 0.1, 'mode': 'FRACTION'}
rebalance = {'region': region_name, 'kind': 'REBALANCE',
             'groups': [g0, g1], 'factors': {g0: 0.8, g1: 1.2}}
smooth = {'region': region_name, 'kind': 'SMOOTH',
          'groups': [g0, g1], 'iterations': 1}
```

ASSIGN 的每个值和 TRANSFER_AMOUNT 的 amount 可为 scalar 或与该 region.ids 等长数组。TRANSFER_AMOUNT 的 mode 仅 ABSOLUTE/FRACTION；超源预算拒绝不 clamp。REBALANCE 因子完整非负。SMOOTH 次数为严格 int 1..100，按 read_ids/barriers 同步迭代，每轮 alpha 一次。高级 FIELD 使用 `field`，形状 len(ids)×len(groups)，有限非负，并按该点原 M 缩放后混合；不是补造质量的入口。

## 4. 保存、加载与同输入显式绑定

```python
from pathlib import Path

def save_and_reload(recipe, targets, new_absolute_path):
    # 目录已存在，文件必须未存在。此函数不执行权重修改。
    wr.save_recipe(recipe, new_absolute_path)
    loaded = wr.load_recipe(new_absolute_path)
    return wr.bind_recipe(loaded, targets)
```

`bind_recipe` 返回新 dict，仅支持几何/坐标、变换、组、rig、单位等仍一致的显式对象映射；不同拓扑不能套旧 RAW IDs。镜像 pairing 有额外绑定，不能指望 region 重绑自动修好 pairing。加载 JSON 不恢复 live Plan，必须重新 prepare。`load_recipe` 文件上限 16 MiB，拒绝重复 JSON 键和非有限数值。

核心 `save_recipe/load_recipe` 只读写 JSON，不主动清所有 Plan；人工 UI 的“保存配方”和加载流程会结束其 live Plan。保存/加载 .blend 的生命周期 handler、卸载及显式 Scene 失权也会结束计划。AI 最清晰的用法是先 cancel 未提交计划，再保存配置，后续重新 prepare。

## 5. 镜像：构建→审阅→确认→使用

```python
def propose_pairing(target, left_ids, right_ids, tolerance_meters,
                    center_ids=()):
    return wr.build_mirror_pairs(
        target, left_ids, right_ids, axis=0, plane_meters=0.0,
        tolerance_meters=tolerance_meters, center_ids=center_ids)

def make_copy_recipe(target, proposed, source_to_target_groups):
    # 仅在调用者已看过配对报告/线段并实际接纳后调用。
    pairing = wr.confirm_pairing(target, proposed)
    right_ids = [right for left, right in pairing['pairs']]
    region = wr.capture_region(target, indices=right_ids, rings=0)
    mapping = dict(source_to_target_groups)
    return {
        'schema': wr.SCHEMA,
        'regions': {'mirror_right': dict(region, target='body')},
        'stages': [{'label': '经确认的左到右复制', 'operations': [{
            'region': 'mirror_right', 'kind': 'MIRROR',
            'groups': list(mapping.values()), 'mode': 'COPY',
            'pairing': pairing, 'group_mapping': mapping,
        }]}],
    }
```

点按 RAW 中立世界坐标、米制单位和明确平面配对；完整一对一 mutual-nearest，容差内多候选算歧义。不能手写 `confirmed=True` 代替 `confirm_pairing`。COPY 固定左→右，映射须覆盖供侧正权列，目的唯一且恰等于 groups。上例如含中心自配，COPY 只允许全映射自映射；左右互换应另用 AVERAGE。

AVERAGE 的 region.ids 应包含实际打算写的配对点，可两侧；`mode='AVERAGE'` 且 group_mapping 必须对合（A→B、B→A，自映射也可）。两侧正质量要求明确，平均的是映射后的比例，仍保留每个目标点自己的 M。修改配对输入后重新构建与审阅。

## 6. 有界表面转移

```python
def surface_operation(region_name, donor, groups, mapping,
                      max_distance_meters, donor_ids=None):
    return {
        'region': region_name, 'kind': 'SURFACE_TRANSFER',
        'groups': list(groups), 'donor': donor.name,
        'donor_ids': donor_ids,
        'max_distance_meters': max_distance_meters,
        'bone_mapping': dict(mapping),
    }
```

把返回操作加入已有 recipe 的 Stage，再 prepare。donor 必须是独立受支持同 rig 输入，不能等于该 Plan 任一 target。distance 为有限正米值；donor_ids 可省略/None，提供时是合法 RAW IDs，供体投影使用完整 RAW 三角。显式 mapping 覆盖参与供体场正权列、全部可写目标组，且目的唯一、均为现有实际 deform 组；映射到其它非写列的项保留在范围说明中，不因此授权写那些列。所有目标点需要有效有界覆盖，否则拒绝。供体状态被冻结，prepare 后变化会使提交拒绝。

## 7. 跨角色批次

```python
def apply_roles(human_targets, human_rig, human_recipe,
                dog_targets, dog_rig, dog_recipe, should_cancel):
    return wr.run_batch([
        {'label': 'human', 'targets': human_targets, 'rig': human_rig,
         'recipe': human_recipe, 'name_prefix': '人形修权候选'},
        {'label': 'dog', 'targets': dog_targets, 'rig': dog_rig,
         'recipe': dog_recipe, 'name_prefix': '犬修权候选'},
    ], continue_on_error=False, cancelled=should_cancel)
```

`should_cancel` 为无参 callable，返回 bool。取消仅在角色间检查；不承诺中断当前同步算子。回执 schema 为 `rigging.weight-repair.batch.v1`；`results` 各项 status 为 succeeded/failed，成功 outputs 是对象名列表，另含 report；失败含 error。`unprocessed` 统计未处理项，`atomicity='per_role_candidate_set'`。仅取消分支附总体 `status='cancelled_between_roles'`，正常返回不保证有总体 status。默认失败停止；continue_on_error=True 才继续。保留成功项，不自动重跑或撤回已完成角色。

## 8. 程序化 Scene 切换

```python
import bpy

def switch_scene_explicitly(next_scene):
    wr.invalidate_scene_context()  # 必须在切换前调用，永久结束旧计划
    bpy.context.window.scene = next_scene
    bpy.context.view_layer.update()
    # 在新 Scene 重新映射、捕获/检查输入并 prepare；不要继续用旧 plan。
```

以上是有 Window 的显式切换示例，不能假设后台总有可操作 Window。后台公共 CLI 每个工作单元单 Scene；另开工作单元/进程处理其它 Scene。保存、加载、受支持通知和 unregister 会清提交权；结束计划清其 owned 预览，已创建独立候选保留。

同步直接 RNA A→B→A 无通知且最终精确恢复的历史不可观测；不宣称自动检测任意瞬时往返。若在 B 实际访问旧 Plan 会永久失权，按公开失权入口切换也有明确保证。不能用返回 A 或恢复对象名来复活已结束 Plan。`release()` 是模块生命周期收尾，取消所有本模块计划并撤 handler/msgbus，不要当作普通配方完成步骤随意调用。

## 9. 隔离诊断真实签名

```text
compare(source, candidate, rig, *, vertex_indices, bone_names, bone_layers,
        poses, output_directory, resolution=720, phase='FIRST',
        reviewed_gate=None, reviewed_receipt=None, views=None,
        delta_limit=.05)
```

只能在 `bpy.app.background` 的独立已保存工作副本/进程调用。该函数会改变后台场景姿态、相机、显示、颜色属性及渲染设置，并创建观察对象；不负责恢复现场，所以不得在用户 GUI 会话绕过后台门。保存源与候选的独立快照，记录来源，启动独立后台入口；本指南不编造新的 CLI。已有公共 CLI 执行方式需按当前 `extension.run` 契约另行准备。

前后对象须同 RAW 几何/拓扑、同 rig、相同变换/修改器栈/Key 输入及组顺序。诊断要求求值后仍与 RAW 同点数；若生产栈有改变拓扑的 Subsurf，应在独立诊断副本对前后双方建立相同、明确的同拓扑观察输入，不能静默修改源文件或只改一侧。

`bone_names` 是两侧共有的实际 deform 权重组。`bone_layers` 的键仅 CONTROL/MECHANISM/DEFORM/OTHER，值为明确真实骨名列表；DEFORM 层必须为 use_deform 骨，不靠骨名前缀猜角色。每个 pose 有 label，可有 frame/subframe、controls、properties。controls 允许 rotation_mode/location/scale/rotation_euler/rotation_quaternion，角度用弧度；properties 仅已有有限标量自定义属性。不要用矩阵回写代替原生通道采姿。

下面包装器每次后台进程只调用一次。对象与 `roi_ids/weight_bones/layers/pose_specs/view_spec` 都由该已保存工作副本的实际诊断清单提供：

```python
from rigging_workflow import weight_repair_diagnostics as evidence

def capture_worker(source, candidate, rig, roi_ids, weight_bones,
                   layers, pose_specs, view_spec, new_absolute_directory,
                   phase='FIRST', gate=None, first_receipt=None):
    return evidence.compare(
        source, candidate, rig,
        vertex_indices=roi_ids,
        bone_names=weight_bones,
        bone_layers=layers,
        poses=pose_specs,
        output_directory=new_absolute_directory,
        resolution=720, phase=phase,
        reviewed_gate=gate, reviewed_receipt=first_receipt,
        views=view_spec, delta_limit=0.05)

def pose_schema_example(existing_control_name):
    # 仅展示通道语法；具体轴和角度必须由实际控制器机制验证。
    return [
        {'label': 'neutral', 'frame': 1, 'subframe': 0.0,
         'controls': {existing_control_name: {
             'rotation_mode': 'XYZ', 'rotation_euler': [0.0, 0.0, 0.0]}},
         'properties': {}},
        {'label': 'small_angle', 'frame': 1, 'subframe': 0.0,
         'controls': {existing_control_name: {
             'rotation_mode': 'XYZ', 'rotation_euler': [0.15, 0.0, 0.0]}},
         'properties': {}},
    ]
```

ROI 至少三个有界整数 RAW IDs，尽量含完整观察三角及外环；空/不完整三角域不是可见性证明。views 为有序 dict，如 `{'rear_quarter': (1, 2, .6), 'side': (1, 0, .12)}`，方向是否适合必须调查，不能按标签认定正背面；标签仅安全字母数字/下划线/短横线。第一个视角就是 FIRST 视角。每次 output_directory 必须全新、绝对路径，不复用已有目录。resolution 严格 int 128..2048。

### SURVEY → FIRST → 人工/主代理看图 → FULL

1. 新后台进程从同一快照调用 `capture_worker(..., phase='SURVEY')`。它计算相机覆盖、像素及三角重心可见性，写 `visibility-check.json/receipt.json`，无图片，status 为 visibility_survey_only。若视角不合适，明确改 views，重新用新目录调查。
2. 另一新进程从同一干净快照调用 `phase='FIRST'`。首视角的首姿态需有可见 ROI 重心样本；每姿态前后至少有一个列出视角见到样本。返回 first_group_pending_review；只采首姿态/首视角组，另含骨层总览。实际打开所有必要首组图，核皮肤、RAW、各骨权重、组和、delta、局部骨链及总览。首图失败停止铺图。
3. 实际审阅接纳后，记录 FIRST `receipt.json` 的 gate 和绝对回执路径。不能在代码里自动把 FIRST 返回值当成批准，也不能因为文件存在就传门。
4. 又一新进程从同一快照、同一全部参数调用 `phase='FULL', gate=已审gate, first_receipt=首组回执路径`，并使用另一个全新输出目录。FULL 核 gate、FIRST 回执及实际图像哈希，复用首组有效图，其余按姿态/视角采集。改变输入、snapshot 路径/文件身份、参数、pose、视角、显示条件或版本使 gate 不匹配时，应重新 FIRST，不能手改 gate。

不要在同一已被诊断改过的后台 Scene 直接连跑 FIRST/FULL，否则颜色/表面/场景等变化可能影响输入签名。每次从同一已保存快照重开是明确的复现路径。源盘不保存诊断现场；失败/取消保留独立证据并标未完成。

可见性 scope 是 `discrete triangle centroid rays; not complete visibility proof`；投影入框不等于可见，少量样本通过不证明整片无遮挡。查看 skipped_views，骨层总览也不代替局部遮挡判断。输出含皮肤前后/逐骨/组和/delta PNG、全组选点 CSV、NPZ、bones JSON、visibility-check 和 receipt。FULL status 为 captured_pending_visual_review，技术完成不代表肩胯改善。

## 10. 回执、失败与交付边界

`plan.report` 记录每目标 binding、Stage 过程、变更点/cell、float32输出摘要、质量漂移、prepare/apply 计时及最终候选名称。candidate 的 `rigging_weight_repair_v1` 自定义属性保存 recipe/report；它是审计信息，不是恢复提交权的 token。检查返回 JSON/报告不代替实际候选保存和独立重开。

apply 成功后状态 applied；失败正常清理为 failed_no_candidates，清理未闭合为 rollback_incomplete，须读 cleanup_errors。cancel 清本计划 owned 范围预览，原源保留。回退是继续使用原源，不自动删除候选、不替换工程或保证动画迁移。

数值通过、精确冷开、性能观测与视觉质量分别报告。真实肩胯修复和完整产品验收以本批主报告为准，本指南不签署尚未完成的质量结论。
