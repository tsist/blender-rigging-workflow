# 0.42.3-rc1 / 2026-11-16

- 从冻结final-ui215成员形成传统ZIP候选；算法/UI原字节保持。
- 仅版本元数据更新；新增当前三指南/RC状态，重建当前来源与成员身份。
- 人工仅N键/侧栏/登记/撤销有限通过；正向helper/Q5及本RC原生加载启停仍待完成。
- 未安装发布，不签母WM完成。下列保留历史版本记录。

# 文档候选 024 变更记录

## 0.42.2统一候选说明（2026-11-13）

合并041：生成中立以原rest采样点的实际B-Bone消费作有限接受证据，完整旧baseline/check_current及旧失败保持。合并045 candidate002：实际选段起点校准、默认关闭的人工范围表单、Euler零符号严格恢复；共享Adapter/guards不变。更新版本、当前指南、SOURCE-MAP与manifest。组合运行结果另见049精确包外报告；无生产安装/发布或肩胯质量补签。


## 0.41.0 后续开发增量（尚未最终封包）

多来源代理增加来源几何保护与完整覆盖拒绝；Tripo低模改走云端重拓扑，已有权重明确TRANSFER后进行逐骨权重/骨架联合诊断。低模真实质量与最终包验收仍待完成，不将历史高模通过项外推。

- 批次 / 角色 / 分片：`rigging-modular-20261112-v001` / `writer` / `docs-024`
- Attempt：`1`
- 日期：2026-11-12（Europe/London）
- 工作目录：`<PROJECT_ROOT>/work/rigging-modular-20261112-v001/integration-review-shard/docs-candidate-024`
- 性质：只读既有证据后的**文档候选**；未改主代码、WM、AGENTS、安装或用户偏好，未运行 Blender、未安装、未联网、未收费、未递归委派。

## 0. 基线与范围

本目录初始为空。为满足“更新 3 份现有文档候选”且不回退 019/020 已整合内容，本次以主包当前 0.41.0 的三份文档（2026-11-12 17:02–17:07 快照）为基线复制到本目录，再按 021–023、`native-surface-validation-shard/REPORT.md` 和 `retarget-shard/FULL-SOURCES-019.md` 做增量更新。基线本身已包含旧 C94 表、018/019/020 正文和 4B 初稿，因此比回到 018 文档候选更能保留已审内容。

| 基线文件 | 字节 | SHA256 |
|---|---:|---|
| `rigging_workflow/SUPPORT-MATRIX.md` | 12,587 | `9358EA47E434A1C75ABE5A76EEACF6E7D3EC527D3D457EF79DD079AA57F3D2BA` |
| `rigging_workflow/MODULAR-USER-GUIDE.md` | 17,395 | `5867A6C6267378AD809727F8B82084C555751ED566FD7C92468BFB8943505F12` |
| `rigging_workflow/MODULAR-AI-CALLS.md` | 23,435 | `7242BEA40EDED3CBBEF4AEA34EE436F23CBC62450491DD99E84E343E32BCB58E` |
| `rigging_workflow/__init__.py`（版本核对） | 4,419 | `2CA3C54ADFC2D8BDAD9A1322F6EF5627EDAED678093B77053634F0F74B4B74C6`，`bl_info.version=(0,41,0)` |

只读源与证据快照：

| 来源 | SHA256 |
|---|---|
| `rigging_workflow/modular_surface.py` | `A4950433869CA03D2255DA3A2DA8AA2FF72CF7691BE069E79E36F0FC621C9201` |
| `rigging_workflow/modular_ui.py` | `E293AA694C9E727D5F3178BC54CBE0069E88B394AF16FB0ACC2EF518414D7564` |
| `native-surface-validation-shard/REPORT.md` | `38577362C3E8FFBCC4C1E8D439E6636F0F58867B01B7FF603E6CF7FBB47BE41D` |
| `retarget-shard/FULL-SOURCES-019.md` | `DEB091CB655C70B8EB97216575AC788DA1AA9A5384741D1F954C8A5A659E4BEA` |
| `retarget-shard/REPORT.md` | `5222A70A07BE7170C41D5BA9BCCD67BD58F878B1B61D2D273588DE0DE6884ED6` |
| `work/audit/<batch>/` | `ACB83647EC4B34899451F4ED6F95C4DA18D4A3CF55E3184784EABF7757046ABA` |
| `A/jobs/079f.../output-native-surface-core.json`（021） | `D7B4481F3AE990D3B5008FD46553E5A7E7DD413563208460090E55C92058542B` |
| `A/jobs/31a1.../output-native-surface-core.json`（022） | `8418262AF8C1824311E1C9C3720D815A145A41EDD8C2FD1BFFF70059B3661542` |
| `A/jobs/57c7.../output-human-surface-example.json`（023） | `DE186F8B48585BD57F6F01701E9870F1171F2E815F73DD0EF984B688D25FAC3B` |

## 1. 逐文件处理与理由

### 1.1 `SUPPORT-MATRIX.md`

| 处理 | 理由 | 来源 |
|---|---|---|
| 新增“021–023 原生平滑增量”段 | 让支持表直接说明新入口、参数、证据和限制，不把表面后处理混进旧权重结论 | `modular_surface.py`、021/022/023 结果、`native-surface-validation-shard/REPORT.md` |
| `当前结论`加入 CMU 344/MMD 1441 两条具名全片、自动/人工髋位比较仍在活动 | 修正“完整 CMU/MMD 长片未验”的旧口径，同时不代签髋位比较 | `FULL-SOURCES-019.md`、`IMPLEMENTATION-CHECKPOINT.md` |
| 新增“可选原生姿态修正平滑（仅人形）”行 | 把 API/GUI、固定配方、恢复、真实数值和不签视觉的边界集中在一行 | 同 021–023；源码 `modular_surface.py:30-105`、`modular_ui.py:46-48/332/472` |
| 更新“人形动作重定向”行 | 增加 CMU 344、MMD 1441 全片保存/冷开、残余和有限皮肤观察；保留 547 Mixamo 与 UI 短入口 | `FULL-SOURCES-019.md` §2–8 |
| 更新“Profile、MMD 与泛型文件”行 | 保留 bounded 5 帧历史，同时把已验的两条具名全片与其它家族分开 | `FULL-SOURCES-019.md` §0/§8 |
| 更新“安装与最终交付”行 | 明确 021–023 仍是审计候选，最终安装/metadata 未验 | `REVIEW.md` §14、本批审计状态 |
| 更新“人工 GUI、旧对象与交接”行 | 删去“019 UI 整链仍在进行”的过时说法；保留肩机制与更广人工接手未验收 | 020 段、`IMPLEMENTATION-CHECKPOINT.md` |
| 扩充“证据入口”并加 `W`/`A` 路径图例 | 让新增 job/报告路径可定位且不改变旧入口 | 实际 `Test-Path` 核对 |
| 顶部把不存在的 `MODULAR-ACCEPTANCE.md` 改为“本表、本批审计报告和交付索引” | 避免文档指向当前树中不存在的文件；未创建该文件（超出本次写入范围） | `Test-Path=False` 于 `rigging_workflow/MODULAR-ACCEPTANCE.md` |

**旧 C94 正文保护**：从 `本次稳定性与工作台整理` 到文件末尾的旧 C94/MMD/FK/Profile/队列正文与基线**逐字一致**，片段 SHA256 `6EA9C56D76548A31E43C4A05CDFBFC0866A38CB2CFBC9605692E97DD2A0F215E`。没有删改旧 0.40.0 表、旧 C94 结论或“安装与最终交付”之外的无关段落。

diff 概要（对基线）：`10 insertions, 7 deletions`。

### 1.2 `MODULAR-USER-GUIDE.md`

| 处理 | 理由 | 来源 |
|---|---|---|
| 重写 4B | 补 UI 入口、`surface_pin_boundary` 默认 True、core 显式 bool、仅 HUMAN、固定配方、真实证据、犬拒绝和恢复 | `modular_ui.py`、`modular_surface.py`、021–023 |
| 4B 明确“只平滑微折” | 防止把 2.01/2.93 mm 或 p99 数值当作原权重/宏观肩胯已修复 | `native-surface-validation-shard/REPORT.md` §3/§8 |
| 第 6 节增补 CMU 344/MMD 1441 全片 | 用真实绑定皮肤、保存/冷开、残余和有限皮肤观察更新旧“长片未验”口径 | `FULL-SOURCES-019.md` §2–8 |
| 第 6 节保留其它未验 | 不把两条具名片段扩成全部 CMU/MMD、面部、物理、FBX importer 或 generic BVH | `FULL-SOURCES-019.md` §8 |
| 更新“当前验证边界” | 汇总 019–023、两条具名全片、肩胯未通过、最终安装/metadata 未验、自动/人工髋位比较仍活动 | 本批证据 |
| 顶部把不存在的 `MODULAR-ACCEPTANCE.md` 改为本文边界+本批审计报告 | 避免悬空引用 | 同上 |

**保留**：第 1–3 节、4A、第 5 节、第 7–8 节的安装、定位、代理/绑定、TRANSFER/RIGID、结构变更、保存/回退正文未改；C94 并存说明未改。

diff 概要（对基线）：`13 insertions, 7 deletions`。

### 1.3 `MODULAR-AI-CALLS.md`

| 处理 | 理由 | 来源 |
|---|---|---|
| 状态段加入两条具名全片与自动/人工髋位未签 | 先给读者当前边界，避免只看到 547 Mixamo | `FULL-SOURCES-019.md` |
| 第 5 节新增“CMU 344 / MMD 1441 具名全片”表 | 给出范围/时基、Action/残余、保存/冷开、有限皮肤观察 | `FULL-SOURCES-019.md` §2–8 |
| 明确 CMU `joint_direction_residual_max=89.240192°` 的源退化原因 | 防止把无效角度当成目标折断，也不修改原字段 | `FULL-SOURCES-019.md` §0/§3.1 |
| 第 6 节补 `modular_surface.create_candidate(source, rig, *, name, pin_boundary)` | 精确 kw-only 签名；无 Python 默认值，必须显式 bool | `modular_surface.py:30` AST/源码 |
| 补 `rigging.modular.surface.v1` report 字段、`artistic_approval=None`、恢复字符串 | 让调用者能识别候选与非视觉批准 | `modular_surface.py:95-105` |
| 补 `surface_pin_boundary`、`surface_candidate`、`rigging_modular.create_surface_candidate` 和面板条件 | 明确人工入口默认 True、仅 HUMAN | `modular_ui.py:46-48/332/472-474` |
| 补 021–023 证据与犬拒绝 | 让文档数字与现有 job 对齐，不把数值签成视觉 | 021/022/023、`native-surface-validation-shard/REPORT.md` |
| 补“关闭命名修改器/回原蒙皮”的恢复路径 | 满足可逆使用与后续权重转移要求 | 源码 report `recovery`、UI 消息、023 结果 |
| 更新“当前验证边界” | 汇总 019–023、两条具名全片、肩胯/安装/metadata/髋位未验 | 本批证据 |
| 顶部把不存在的 `MODULAR-ACCEPTANCE.md` 改为本批审计报告 | 避免悬空引用 | 同上 |

**保留**：`prepare_reference/compose_reference/confirm_reference/generate/read_*`、定位、体素绑定、分件身体、TRANSFER/RIGID、`modular_retarget.convert`/`convert_profile`、preview 签名与 C94 说明均未改；只加全片证据段、表面段和边界。

diff 概要（对基线）：`34 insertions, 6 deletions`。

## 2. API/属性与实际源码核对

只读 AST/源码核对，未运行 Blender：

| 文档陈述 | 源码依据 | 结果 |
|---|---|---|
| `create_candidate(source, rig, *, name, pin_boundary)` | `modular_surface.py:30`；AST 确认仅 `source/rig` 位置参数、`name/pin_boundary` 为 kw-only、二者 `kw_defaults=None` | 一致 |
| `pin_boundary` 必须显式 bool | `modular_surface.py:35-36` 使用 `type(pin_boundary) is not bool` 拒绝 | 一致 |
| 只接纳 HUMAN | `modular_surface.py:44-45` 检查 `configuration.kind != 'HUMAN'` | 一致 |
| 拒绝叠已有候选 | `modular_surface.py:14` `KEY='rigging_modular_surface_v1'`，`:53-54` 拒绝 | 一致 |
| 固定配方 | `modular_surface.py:66-72`：factor .5、iterations 5、scale 1、SIMPLE、BIND、pin 显式 | 一致 |
| 修改器顺序 Armature→Corrective→Subsurf | `:64-65` 新建后移至 index 1；前置 `_bound` 要求 index 0 为 Armature、其余仅 Subsurf | 与源码前置合同一致 |
| report schema/字段/`artistic_approval=None`/recovery | `modular_surface.py:95-105` | 一致 |
| UI `surface_pin_boundary` 默认 True、名称“固定开放边界” | `modular_ui.py:47-48` | 一致 |
| UI `surface_candidate` 指针 | `modular_ui.py:46` | 一致 |
| UI 操作符 `rigging_modular.create_surface_candidate` | `modular_ui.py:332-334` | 一致 |
| 仅有人形候选时显示 | `modular_ui.py:472` | 一致 |
| `modular_retarget.convert(...)` 签名 | `modular_retarget.py:276-279`；AST 核对 kw-only 与默认值 | 一致（原文未改） |
| `convert_profile(source, target, profile, **kwargs)` | `modular_retarget.py:304-306` | 一致（原文未改） |
| preview 三函数 | `retarget_action.py:330/375/73` | 一致（原文未改） |

## 3. 数值与证据核对

- 021 核心：16,026 点 HUMAN partial 夹具，9 项 checks 全 true；两种 pin 的 neutral error 均为 `2.4578447942743725e-7 m`；关闭修改器逐点等于原源；非中立拒绝；绑定后受控失败回滚；源/rig 状态保持。
- 022 UI：同一夹具 11 项 checks 全 true；新增 `real_ui_operator`、`ui_pointer_role_and_recipe`；操作符为 `rigging_modular.create_surface_candidate`，`surface_pin_boundary=True` 写入修改器并可读回。
- 023 真实人形：722,809 点 C017 绑定皮肤，`neutral 9.071674691055656e-08 m`；`left_hip45 0.0020057802038870695 m`；`shoulder60 0.0029316020324516457 m`；三姿态 `disable_exact=true`；`input_untouched=true`；`native_driver_count=89`；`artistic_approval=null`。
- 犬：`native-surface-validation-shard/REPORT.md` §7.3，pin=True shoulder60 max `3.390842→5.245375`；当前源码 HUMAN guard 拒绝四足。文档只陈述该产品决定与数值，不把犬图当视觉通过。
- 重定向：CMU 344 帧 `121 fcurve/41,624 keys`，MMD 1,441 采样 `121 fcurve/174,361 keys`；两条均保存/独立冷开，目标 196 骨/89 drivers/722,809 点皮肤保持；CMU 残余 `6.484163/6.484283°`，MMD spine `42.844950/42.844967°`；各有 7 代表帧×3 视角共 21 张皮肤近景/contact sheet。具体限制以 `FULL-SOURCES-019.md` §8 为准。

## 4. 引用路径核对

以下新增/更新的关键引用均 `Test-Path=True`：

- `W/retarget-shard/FULL-SOURCES-019.md`、`W/retarget-shard/REPORT.md`、`W/retarget-shard/evidence/full-sources-019/`
- `W/native-surface-validation-shard/REPORT.md`
- `A/jobs/079f061ea21a4e7686fca5e1f0add2ea/output-native-surface-core.json`
- `A/jobs/31a1941fae5841a9acb05cb86ef0cb99/output-native-surface-core.json`
- `A/jobs/57c7cc0162e44c5298a1ba4d650a768c/output-human-surface-example.json`
- `A/jobs/bb379155898c42ac9eeb5017b248665d/output-manual-chain-cold.json`
- `A/jobs/c3259ac35885493b90c65a9fb75dac83/output-display-ui.json`
- `A/jobs/0b44df38c08d4afea39d91f793edd2f2/output-parts-cold.json`
- `A/jobs/d72f9c5433a0461189719b4697e8f407/output-template-cold.json`
- `A/jobs/59cc1a75f42a4aa6813b164928436286/output-dog-cold-reopen.json`
- `A/jobs/718ec7a2fabe4f63820d3c7ead3b84f5/output-motion-ui.json`
- `A/jobs/92072a966e8d4192ace8b3c7bbe71e6c/output-motion-ui.json`
- `A/jobs/b2d94adaa808411e8f05d94b205f273f/output-binding-report.json`
- `A/evidence/canine-017-production-projection.json`
- `A/jobs/3fca6c36f87748689bb9398a4d3d7324/output-install-report.json`
- `A/delivery-candidate-018/templates/TEMPLATES.json`
- `W/module-combinations-shard/REPORT.md`、`W/module-combinations-shard/SUMMARY.json`
- `W/retarget-shard/evidence/bound-skin/`、`W/retarget-shard/evidence/profile-bridge/`

路径图例：`W`=`<PROJECT_ROOT>/work/rigging-modular-20261112-v001`；`A`=`work/audit/<batch>/`。

## 5. 明确未验与剩余问题

1. **本任务只写文档，未重跑 Blender**。021–023 和两条重定向全片的数值都来自既有审计/分片产物；本次只读回并核对路径与报告。
2. **HUMAN guard 的版本差异**：当前主包 `modular_surface.py` 与 023 job 副本均为 `A495...`，含 `:44-45` 的 HUMAN guard；021/022 job 副本（`c1cdf40...`）在该处无此检查。故“产品入口拒绝四足”由当前源码/023 副本与犬分片的高尾报告共同支持，但 023 仍只跑了人形三姿态，没有犬运行。021–023 的人形数值不受该两行 guard 影响。
3. `native-surface-validation-shard/REPORT.md` 审阅的核心 SHA 为 `0e017dbb...`，当前主包/023 为 `A495...`；`modular_surface.py` 的差异是新增 HUMAN guard（UI 接入在另一个文件）。本文按当前源码核对 API/属性，数值仍引用 021–023。
4. **最终安装/metadata 未验**：candidate018 的 `SOURCE-MAP`/`runtime-manifest` 仍缺 13 个 modular/retarget 文件，不能当最终包；最终冻结后仍需 `finalize_package.py` 重建并重打 ZIP/seal。
5. **肩胯质量未通过**：视觉肩腋/髋前仍未批准；原生平滑只减轻微折，不修原权重、宏观肩窝/髋部凹陷或穿透；微三角 normal flip 仍存在。
6. **自动髋位与旧人工髋位的比较仍在活动**，本文不代签其结果。
7. **重定向范围只限两条具名片段**：CMU `02_01_Rig` 344 帧、MMD `MS_Uka_arm` dance 1,441 采样；其它 CMU/MMD 长片/家族、四足、面部/morph/gaze/物理、FBX importer 端到端、generic non-CMU BVH、连续接触/足底滑动均未验。CMU `6.484°`、MMD spine `42.845°` 残余保留。
8. `MODULAR-ACCEPTANCE.md` 在当前主包不存在；三份文档顶部引用已改为“本批审计报告/本文边界”，未创建该文件（超出本次 4 文件写入范围）。

## 6. 文件 SHA256（本候选最终态）

| 文件 | 字节 | SHA256 |
|---|---:|---|
| `SUPPORT-MATRIX.md` | 15,866 | `891296285DC0C62F1DF744C470D0E0DD49F135C59384AD40FDE191A3BD842BC2` |
| `MODULAR-USER-GUIDE.md` | 19,701 | `7CE512948E9725FD3363332AF6EE4D652214CF592A4F9B4F778AD12CC3327E9B` |
| `MODULAR-AI-CALLS.md` | 28,251 | `ED8D4E0BE69585756DF9E2E22C510A31C36AAB653F46E884587B2CAE2943E928` |
| `CHANGELOG.md` | 见外层回执 | 由外层回执给出；文件不能自包含会改变自身的哈希 |
