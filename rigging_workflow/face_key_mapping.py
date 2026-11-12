# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit existing-Key mappings on independent copies, never source renames."""
import json
import uuid
import bpy
from . import face_capture as capture, face_expression_controls as controls, face_modes as modes

STATE_KEY='c94_existing_key_mapping_v1'


def create(rig, sources, mapping, *, name='已有Key表情控制'):
    """mapping: ordered {name, keys:[actual Key name per explicit source]} rows.

    Requires ordinary static relative Keys and a measured KEY neutral. Copies
    retain all old Key names/coordinates/relationships; only copies get drivers.
    """
    require=controls._require
    require(isinstance(sources,(list,tuple)) and sources and len(set(sources))==len(sources), '明确提供不重复的源Mesh列表')
    require(isinstance(name,str) and name.strip() and len(name.encode('utf8'))<=63 and name not in bpy.data.objects,'控制对象名称无效或已存在')
    require(isinstance(mapping,list) and mapping and all(isinstance(r,dict) and set(r)=={'name','keys'} for r in mapping),'明确提供语义名称及每个源的实际Key映射')
    require(all(isinstance(r['name'],str) and r['name'].strip() and len(r['name'].encode('utf8'))<=63
                and isinstance(r['keys'],list) and len(r['keys'])==len(sources)
                and all(isinstance(n,str) and n for n in r['keys']) for r in mapping),'映射名称/成员数量无效')
    require(len({r['name'] for r in mapping})==len(mapping) and all(len({r['keys'][i] for r in mapping})==len(mapping) for i in range(len(sources))), '语义名或目标Key重复')
    fingerprint=controls.sig._hash(modes._read(rig)['neutral']);controls._baseline(rig,fingerprint)
    before=[]
    for i,obj in enumerate(sources):
        session=capture.begin(obj,rig)
        try:
            keys=obj.data.shape_keys
            require(keys is not None,'源没有已有Key')
            for row in mapping:
                key=keys.key_blocks.get(row['keys'][i])
                require(key is not None and key!=keys.reference_key,'映射Key缺失或指向Basis：'+obj.name)
                require(not key.mute and key.slider_min<=0 and key.slider_max>=1,'映射Key须未静音并支持0..1范围')
            before.append(capture._source_state(obj))
        finally:capture.cancel(session)
    copies=[];hub=None;token=uuid.uuid4().hex
    try:
        for i,obj in enumerate(sources):
            copy=obj.copy();copies.append(copy);copy.data=obj.data.copy();copy.name='Key映射副本_'+token[:8]+'_%02d'%i
            bpy.context.scene.collection.objects.link(copy)
            copy[STATE_KEY]=json.dumps(dict(source=obj.name,mapping=mapping,source_fingerprint=before[i]),ensure_ascii=False)
        channels=[dict(name=r['name'],property='表情_%02d'%i,keys=list(r['keys'])) for i,r in enumerate(mapping)]
        hub=controls._bind(rig,copies,channels,group_id='existing-key-'+token,fingerprint=fingerprint,name=name)
        for i,obj in enumerate(sources):hub['author_source_%03d'%i]=obj
        require(all(capture._source_state(obj)==state for obj,state in zip(sources,before)), '映射期间源发生变化，拒绝提交')
        return hub,copies
    except Exception:
        # This entire group is newly owned; sources/old Keys are never touched.
        if hub is not None:bpy.data.objects.remove(hub,do_unlink=True)
        for obj in reversed(copies):capture._discard(obj)
        raise
