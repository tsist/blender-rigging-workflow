# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit rig identity dispatch; generated and adopted identities stay distinct."""
import bpy

C94_KEY = 'c94_generated_rig_v1'
MODULAR_KEY = 'rigging_modular_v1'
NATIVE_KEY = 'rigging_native_skeleton_v1'


def read(rig):
    if (not isinstance(rig, bpy.types.Object) or rig.type != 'ARMATURE'
            or rig.library or rig.data.library or rig.mode == 'EDIT'
            or rig.name not in bpy.context.view_layer.objects):
        raise ValueError('请选择当前视图层的本地生成骨架，并结束编辑模式')
    if sum(key in rig for key in (C94_KEY, MODULAR_KEY, NATIVE_KEY)) > 1:
        raise ValueError('骨架同时含多种身份，请保留原件并使用身份明确的独立候选')
    if C94_KEY in rig:
        from .pose_switching import _metadata
        return _metadata(rig)
    if MODULAR_KEY in rig:
        from .modular_rig import read_generated
        return read_generated(rig)
    if NATIVE_KEY in rig:
        from .native_skeleton import read_generated
        return read_generated(rig)
    raise ValueError('请选择C94、模块化流程骨架，或显式登记的原生独立骨架')


def family(rig):
    data = read(rig)
    if data['schema'] == 'rigging.native-skeleton.v1':
        return 'NATIVE'
    return 'MODULAR' if data['schema'] == 'rigging.modular.generated.v1' else 'C94'
