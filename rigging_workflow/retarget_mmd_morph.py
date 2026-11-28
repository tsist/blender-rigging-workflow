# SPDX-License-Identifier: GPL-3.0-or-later
"""Validated persisted MMD native Key curves -> explicit morph tracks.

No UI registration, MMD Tools import, Action binding, Key write or new keys.
"""
import json
import bpy
from . import face_modes as fm,retarget_mmd_face as face

def _require(value,message):
    if not value:raise ValueError(message)

def _backend():
    from . import mmd_source_backend
    return mmd_source_backend

def _json(raw,label):
    _require(isinstance(raw,str) and len(raw)<=16*1024*1024,label+'为空或超过16MiB')
    try:return json.loads(raw)
    except (ValueError,TypeError) as exc:raise ValueError(label+'不是有效JSON') from exc

def _read_validated_morph_tracks(profile,ctx):
    """Read the already validated clip's native Key Action without MMD RNA."""
    clip=ctx['clip'];tracks={};sources=[]
    for record in clip['shape_key_actions']:
        mesh=bpy.data.objects[record['mesh']];keys=mesh.data.shape_keys;meta=record['action']
        action=bpy.data.actions[meta['name']];slot=next((s for s in action.slots if s.identifier==meta['slot']),None)
        _require(keys is not None and slot is not None and slot.target_id_type=='KEY','来源morph必须有明确Key数据和Slot')
        timebase=_json(action.get(_backend().TIME_KEY),'来源morph Action时基')
        _require(abs(float(timebase['fps'])-ctx['fps'])<1e-9 and slot.identifier in timebase['slots'],'来源morph Action/Slot时基与所选clip不符')
        names={key.path_from_id('value'):key.name for key in keys.key_blocks if key!=keys.reference_key}
        seen=set();drivers=keys.animation_data.drivers if keys.animation_data else ()
        for fc in fm._curves(action,slot):
            _require(fc.data_path in names and fc.array_index==0 and not fc.mute and not fc.modifiers
                     and not fc.sampled_points and fc.keyframe_points and fc.extrapolation=='CONSTANT','只支持明确原生Key值曲线，无mute/modifier/采样点')
            _require(fc.data_path not in seen,'同一morph Action/Slot含重复路径，不猜叠加语义');seen.add(fc.data_path)
            _require(not any(d.data_path==fc.data_path and d.array_index==0 for d in drivers),'来源Key值由driver重写，不能把原曲线冒充实际morph')
            points=[(float(k.co.x),float(k.co.y)) for k in fc.keyframe_points]
            _require(all(k.interpolation=='LINEAR' for k in fc.keyframe_points) or len({v for _,v in points})==1,
                     '动态来源morph必须是已验LINEAR插值，不自动改曲线')
            for frame in clip['range']:
                if not any(f==float(frame) for f,_ in points):points.append((float(frame),float(fc.evaluate(frame))))
            name=names[fc.data_path];points=sorted(set(points))
            _require(name not in tracks or tracks[name]==points,'多网格同名morph权重不一致，不自动合并')
            tracks[name]=points
        sources.append({'mesh':mesh.name,'action':action.name,'slot':slot.identifier})
    _require(tracks,'所选clip没有已匹配的原生morph Action轨道')
    return face._tracks(tracks),{'kind':'validated_profile_native_key_action','profile_sha256':profile['profile_sha256'],
                                'clip_id':clip['id'],'original_vmd':clip['vmd'],'actions':sources}


def morph_tracks_from_profile(source,profile,*,clip_id,source_fps=None,require_scene_config=True):
    """Public Agent/UI input adapter; no MMD Tools import or RNA is required.

    The backend validates the actual source objects, Action/Slot, graph and
    profile before native Key curves are read. Only supported linear morph
    tracks are returned. This function never binds Actions or writes values.
    """
    backend=_backend()
    _require(type(require_scene_config) is bool,'来源Scene检查范围须明确为布尔值')
    profile=backend.load_profile_document(profile)
    backend.validate_persisted_source(profile,scene=bpy.context.scene,expected_rig=source,
                                      require_scene_config=require_scene_config,require_all_actions=True)
    ctx=backend.clip_timebase(profile,clip_id,source_fps=source_fps)
    return _read_validated_morph_tracks(profile,ctx)

