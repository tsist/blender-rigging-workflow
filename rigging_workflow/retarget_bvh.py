# SPDX-License-Identifier: GPL-3.0-or-later
"""Configurable BVH import and strict CMU preset; native source settings persist."""
import json
from pathlib import Path
from . import retarget_source as sources,retarget_calibration as calibration

SOURCE_KEY='c94_retarget_bvh_source_v1'

def import_file(filepath,*,scale=.01,forward='Z',up='Y'):
    """Import configurable BVH; Profile validates semantic mapping separately."""
    imported=calibration.import_bvh(filepath,
        importer_path=str(Path(__file__).with_name('retarget_bvh_source.py')),
        scale=scale,forward=forward,up=up)
    imported['rig'][SOURCE_KEY]=json.dumps({'schema':'c94.retarget.bvh_source.v1',
        'preset':'configured','record':imported['record']},ensure_ascii=False)
    return imported

def import_cmu(filepath,*,scale=.01,forward='Z',up='Y'):
    imported=calibration.import_bvh(filepath,
        importer_path=str(Path(__file__).with_name('retarget_bvh_source.py')),
        scale=scale,forward=forward,up=up)
    sources.exact_cmu_mapping(imported['rig'])
    imported['rig'][SOURCE_KEY]=json.dumps({'schema':'c94.retarget.bvh_source.v1',
        'preset':'cmu_exact_31_joint_sample_v1','record':imported['record']},ensure_ascii=False)
    return imported

def source_context(rig):
    sources.armature(rig)
    try:
        saved=json.loads(rig[SOURCE_KEY])
        sources.require(saved['schema']=='c94.retarget.bvh_source.v1' and
            saved['preset']=='cmu_exact_31_joint_sample_v1','不支持该BVH源配置')
        record=saved['record']
        sources.require(all(k in record for k in ('forward','up','scale')),'BVH轴/单位配置不完整')
    except (KeyError,TypeError,json.JSONDecodeError) as exc:
        raise ValueError('源骨架没有本入口的BVH配置；请用CMU BVH导入入口建立工作源') from exc
    sources.exact_cmu_mapping(rig)
    sources.require(rig.animation_data and rig.animation_data.action,'BVH源须选择活动Action')
    # Clip Action/Slot are deliberately taken from the current scene binding;
    # the persistent record describes only the imported skeleton/axis source.
    return {'rig':rig,'action':rig.animation_data.action,
            'slot':rig.animation_data.action_slot,'record':record}

def convert(source,target,*,mapping=None,source_fps=None,motion='PRESERVE',sample_step=1,
            action_name=None,progress=None,cancelled=None,start=None,end=None,output_start=1):
    exact=sources.exact_cmu_mapping(source)
    sources.require(not mapping or mapping==exact['bones'],
        '当前CMU预设采用已核精确映射；自定义BVH映射尚需独立配置验证')
    return calibration.convert_cmu_bvh(source,target,bvh_import=source_context(source),mapping=exact,
        source_fps=source_fps,motion=motion,sample_step=sample_step,action_name=action_name,
        progress=progress,cancelled=cancelled,start=start,end=end,output_start=output_start)
