# AI同核调用与成功请求复用（0.42.3-rc1）

本页依据当前[UI候选源码](</<PROJECT_ROOT>/work/rigging-wm-final-ui-20261116-v001/rigging_workflow/>)逐项核过签名。当前公开执行入口是项目blenderctl的`extension.run`请求；不要把仍指向v134旧core.service的`rigging-workflow.run`当本候选入口，也不要只改全局locator冒充整合。

## 可复制的真实成功案例

成功任务`9c482097b0684cd68d0ea5ab7b867daf`使用[冻结请求](</work/audit/<batch>/>)，SHA256为`e53acffc7a7205920525ef906275ec4860c5afcb3fdba71b14cac9a239e4015b`。内容明确`command="extension.run"`，脚本和资源均有真实SHA。它与本批request-warm.json解析后内容一致；本次草稿核对七项直接资源、脚本及215成员清单，均无SHA差异。

以下命令仅供复用已验犬输入的验收回放，**本次未执行**。运行会产生一次真实后台求解及一个独立蒙皮候选，不是只读检查，也不适用于当前暂停的袖根分支。保持原冻结输入与源码，使用新的独立作业目录，不编辑旧请求中的SHA使变更输入假装通过。

```powershell
& './blenderctl.ps1' --jobs-dir 'work/audit/<batch>/' --timeout 900 request 'work/audit/<batch>/'
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
```

此请求的实际脚本verify_binding_ui.py在独立后台进程显式导入`<PROJECT_ROOT>/work/rigging-wm-final-ui-20261116-v001`，只注册modular_ui，选择既有犬骨架/RAW/静态参照/11384点代理，真实调用登记→启动→提交operator；含四项明确拒绝检查。它是具名同输入验收脚本，不是任意角色的一键绑定产品接口。

成功回放的预期事实：solver_starts=1、candidate_commits=1；全部5262×29权重及29列SHA与原基线exact、全5262中立求值点exact；中立误差`7.099586277944801e-7 m`。候选与JSON在**新job自己的目录**，不可预先写死job ID。检查CLI退出码、最终status、领域报告及候选实际输出；FINISHED或文件存在不单独代表通过。原实证另有独立纯原生冷开`bfd19edbe24f4144b361683ec9c8f780`，本草稿没有重跑。

## 制作调用应消费的同核

下列签名来自当前候选，不是可脱离Blender直接运行的脚本；scene/source/rig/reference/proxy均须是已核身份的真实bpy对象。新制作输入需要自己的绝对路径、完整资源描述、明确设置和适用回执，不能改上述犬回放请求伪装成另一个角色。

```text
modular_rig.prepare_reference(scene, *, kind='HUMAN', label='基础人形',
                             configuration=None, allow_runtime_enable=False)
modular_positioning.estimate(reference, target_objects, *, forward_axis='-Y', up_axis='Z')
modular_rig.compose_reference(scene, reference, *, configuration, label=None,
                             allow_runtime_enable=False)
modular_rig.confirm_reference(reference, *, allow_runtime_enable=False)
modular_rig.generate(scene, reference, *, label=None, allow_runtime_enable=False)

skeleton_gate.authorize(rig, reference, regions, receipt_path)
skeleton_gate.require_pass(rig, *, operation)
skeleton_gate.revoke(rig)

native_binding.start(source, rig, proxy, bone_names, settings, *,
                     jobs_directory, transfer_distance_cells=3)
modular_workflow.start_binding(source, rig, *, proxy=None, proxy_mode='AUTO',
                              settings=None, jobs_directory, body_sources=None,
                              transfer_distance_cells=3)
```

以上代码块用于展示函数定义形式，不含执行语句。Python默认configuration=None的模块集与新UI默认勾选肩带不同：要复现UI人形肩带，显式enabled_modules中加入`shoulder_girdle`；人形肩带不适用于QUADRUPED。确认参考骨后仍须独立完成骨架QC；登记回执不生成PASS。regions须来自真实完整证据中的全骨分区映射，不能临时造空字典。

NATIVE只接受`rigging.native-skeleton.v1`、显式proxy和完整metadata.deforms，拒绝过滤骨列、分域/debug输出或自动扩scope。MODULAR走`modular_workflow`，不以native桥替换。两种作业句柄均提供status()/cancel()/apply(name=...)；等待实际成功再apply，保有原活句柄，重开文件/磁盘回执不能恢复其提交权。

新UI对NATIVE的设置是cells_longest=resolution、max_cells=600000、max_memory_mb=1024、max_seconds=300、max_iterations=3000、tolerance=1e-6、neighbor_count=26、require_all_bones_seeded=True，回写距离另传transfer_distance_cells；这不等同MODULAR内部默认资源预算。实例回放固定resolution=96、距离3格，不因超距失败自动放大。参数可配置范围不是任意模型已获验收。

## 人形动作候选与恢复

```text
modular_retarget.convert(source, target, profile=None, *, mapping=None, clip=None,
                        source_fps=None, start=None, end=None, output_start=1,
                        motion='PRESERVE', sample_step=1, action_name=None,
                        progress=None, cancelled=None, adapter_factory=None,
                        check_files=True)
retarget_action.begin_preview(target, action, *, slot=None)
retarget_action.stop_preview(target, *, notify=True)
```

当前肩带目标的新增实证限CMU源58..82→25帧；最大完整旋转残差6.83231°，不写scapula独立节律。来源profile、Action/Slot和实际采样范围须明确；preview后stop恢复旧绑定，不把候选自动当正式动画。

面板对应operator为`rigging_modular.authorize_skeleton`、`start_binding`、`cancel_binding`、`apply_binding`、`revoke_skeleton`和`discard_binding_result`；它们调用上述同核，未另写权重算法。真实N键/侧栏/登记/撤销已有限人工通过；鼠标启动/提交/重定向仍未验，不能由编程operator代签。

源码复核入口：modular_rig.py:1562/1622/1723/2381，modular_positioning.py:4859，skeleton_gate.py:96/116/121，native_binding.py:240，modular_workflow.py:314，modular_retarget.py:333，retarget_action.py:330/375，modular_ui.py:289–429。当前UI SHA为`83864ec25a9120b50747ffbf0cb5b1bb90feafc1ece77cfe60607f66525ac73c`。本页冻结成功请求仍指向原215成员final-ui树；RC仅版本/文档身份变化，不能将它冒称RC已原生运行。所有制作风险、暂停项与回退按[用户指南](用户指南与回退.md)和[支持矩阵](支持矩阵.md)执行；本页不替代最终包清单或完成证明。
