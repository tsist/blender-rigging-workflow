# SPDX-License-Identifier: GPL-3.0-or-later
"""Keep old auxiliary motion separate from a newly retargeted base Action.

Only existing managed enable inputs are temporarily changed. No cache, Action,
NLA track, weight, dynamic record or output object is removed or rewritten.
"""
import json
import bpy

def capture(target):
    settings=target.pose.bones.get('骨架设置');properties={};groups=[]
    if 'c94_elastic_v1' in target:
        from . import elastic
        if elastic.session(target) is not None:
            raise ValueError('请先停止胸臀动态RAM预览/冻结，再转换或预览新基础动作')
        record=elastic._record(target)
        if record['bakes']:
            for name,row in record['groups'].items():
                properties[row['property_enabled']]=None
                groups.append({'kind':'ELASTIC','group':name,'outputs':len(record['bakes']),
                               'recapture_entry':'elastic.capture'})
    if 'c94_surface_dynamic_v1' in target:
        from . import surface_dynamic as surface
        record=surface.inspect(target)
        for name,row in record['groups'].items():
            session=surface.session(target,name)
            if session and (session.playing or session.frozen):
                raise ValueError('请先停止发裙动态RAM预览/冻结，再转换或预览新基础动作：'+name)
            if row['bakes']:
                surface._owned_outputs(target,name,bpy.context.scene)
                properties[row['enabled']]=None
                groups.append({'kind':'SURFACE','group':name,'outputs':len(row['bakes']),
                               'recapture_entry':'surface_dynamic.capture'})
    if properties and settings is None:raise ValueError('辅助动态记录存在但骨架设置缺失')
    drivers=target.animation_data.drivers if target.animation_data else ()
    for key in properties:
        if key not in settings or type(settings[key]) not in (bool,int,float):
            raise ValueError('辅助动态启用输入缺失或类型已改变：'+key)
        path=settings.path_from_id()+'['+json.dumps(key,ensure_ascii=False)+']'
        if any(curve.data_path==path for curve in drivers):
            raise ValueError('辅助动态启用输入由外来driver占用，需明确适配：'+key)
        properties[key]=settings[key]
    return {'properties':properties,'groups':groups,'policy':'base_only_recapture_required'}

def suspend(target,state):
    if not state['properties']:return
    settings=target.pose.bones['骨架设置']
    for key in state['properties']:settings[key]=False
    if state['properties']:target.update_tag(refresh={'OBJECT'})

def restore(target,state,*,guard=False):
    settings=target.pose.bones.get('骨架设置')
    if guard and any(key not in settings or bool(settings[key]) for key in state['properties']):
        raise ValueError('预览期间辅助动态开关已被外部改变，保留当前工作并检查恢复记录')
    for key,value in state['properties'].items():settings[key]=value
    if state['properties']:target.update_tag(refresh={'OBJECT'})

def report(state):
    return {'policy':state['policy'],'status':'recapture_required' if state['groups'] else 'no_managed_bakes',
            'groups':state['groups'],'old_outputs_retained':True,'automatic_rebake':False,
            'message':'旧辅助动态未认证用于新基础动作，预览期间暂关闭；按分组重新捕获/烘焙' if state['groups'] else ''}
