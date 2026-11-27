# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit source import and semantic mapping. No production-scene retiming."""
import json
from pathlib import Path
import re
import bpy

SOURCE_KEY='c94_retarget_source_v1'
TIME_KEY='c94_retarget_timebase_v1'
BODY={'hips':'Hips','spine':'Spine','spine1':'Spine1','chest':'Spine2','neck':'Neck','head':'Head'}
FINGERS={'thumb':'Thumb','index':'Index','middle':'Middle','ring':'Ring','pinky':'Pinky'}
for _side,_word in (('L','Left'),('R','Right')):
    for _semantic,_suffix in (('shoulder','Shoulder'),('upper_arm','Arm'),('forearm','ForeArm'),
                              ('hand','Hand'),('thigh','UpLeg'),('shin','Leg'),('foot','Foot'),('toes','ToeBase')):
        BODY[_semantic+'.'+_side]=_word+_suffix
    for _finger,_alias in FINGERS.items():
        for _index in range(1,5):BODY[_finger+str(_index)+'.'+_side]=_word+'Hand'+_alias+str(_index)
REQUIRED={'hips','chest','head',*(s+'.'+side for side in ('L','R') for s in ('upper_arm','forearm','hand','thigh','shin','foot'))}

# Exact CMU sample names.  This is deliberately not the Mixamo BODY preset:
# CMU "Spine1" is the chest, and there is no independent "spine1" semantic.
CMU_BONES={
    'hips':'Hips','spine':'Spine','chest':'Spine1','neck':'Neck','head':'Head',
    'shoulder.L':'LeftShoulder','upper_arm.L':'LeftArm','forearm.L':'LeftForeArm','hand.L':'LeftHand',
    'thigh.L':'LeftUpLeg','shin.L':'LeftLeg','foot.L':'LeftFoot','toes.L':'LeftToeBase',
    'shoulder.R':'RightShoulder','upper_arm.R':'RightArm','forearm.R':'RightForeArm','hand.R':'RightHand',
    'thigh.R':'RightUpLeg','shin.R':'RightLeg','foot.R':'RightFoot','toes.R':'RightToeBase',
}
CMU_REQUIRED={'hips','chest','head',*(s+'.'+side for side in ('L','R')
    for s in ('upper_arm','forearm','hand','thigh','shin','foot'))}
CMU_EXPECTED_PARENTS={
    'Hips':None,'LHipJoint':'Hips','RHipJoint':'Hips','LowerBack':'Hips','Spine':'LowerBack',
    'Spine1':'Spine','Neck':'Spine1','Neck1':'Neck','Head':'Neck1',
    'LeftShoulder':'Spine1','RightShoulder':'Spine1','LeftArm':'LeftShoulder','RightArm':'RightShoulder',
    'LeftForeArm':'LeftArm','RightForeArm':'RightArm','LeftHand':'LeftForeArm','RightHand':'RightForeArm',
    'LeftUpLeg':'LHipJoint','RightUpLeg':'RHipJoint','LeftLeg':'LeftUpLeg','RightLeg':'RightUpLeg',
    'LeftFoot':'LeftLeg','RightFoot':'RightLeg','LeftToeBase':'LeftFoot','RightToeBase':'RightFoot',
}

def require(value,message):
    if not value: raise ValueError(message)

def armature(obj):
    require(isinstance(obj,bpy.types.Object) and obj.type=='ARMATURE' and obj.name in bpy.context.view_layer.objects,
            '请选择当前视图层内的骨架')
    require(obj.mode!='EDIT','请结束骨架编辑模式')
    return obj

def mapping(rig,overrides=None):
    """A name preset is a proposal, validated against real hierarchy below."""
    armature(rig)
    lookup={}
    for bone in rig.data.bones:
        token=bone.name.rsplit(':',1)[-1].lower()
        lookup.setdefault(token,[]).append(bone.name)
    result={}
    for semantic,alias in BODY.items():
        candidates=lookup.get(alias.lower(),[])
        if len(candidates)==1: result[semantic]=candidates[0]
    if overrides:
        require(isinstance(overrides,dict) and set(overrides)<=set(BODY),'映射必须为已知语义到骨名的纯数据')
        for semantic,name in overrides.items():
            if name is None: result.pop(semantic,None)
            else:
                require(isinstance(name,str) and name in rig.data.bones,'映射骨不存在：'+str(name))
                result[semantic]=name
    require(REQUIRED<=set(result),'关键源映射不完整：'+', '.join(sorted(REQUIRED-set(result))))
    require(len(set(result.values()))==len(result),'多个语义不能复用同一根源骨')
    for side in ('L','R'):
        for parent,child in (('upper_arm','forearm'),('forearm','hand'),('thigh','shin'),('shin','foot')):
            p,c=result[parent+'.'+side],rig.data.bones[result[child+'.'+side]]
            require(p in {b.name for b in c.parent_recursive},'源关节层级与语义不符：'+p+' → '+c.name)
    fingers={'complete':[],'skipped':{}}
    for side in ('L','R'):
        for finger in FINGERS:
            chain=[finger+str(i)+'.'+side for i in range(1,5)]
            missing=[s for s in chain if s not in result]
            if missing:
                fingers['skipped'][finger+'.'+side]=missing
                continue
            for parent,child in zip(['hand.'+side]+chain[:-1],chain):
                require(result[parent] in {b.name for b in rig.data.bones[result[child]].parent_recursive},
                        '源手指层级与语义不符：'+parent+' → '+child)
            fingers['complete'].append(finger+'.'+side)
    return {'schema':'c94.retarget.mapping.v1','preset':'mixamo_names_candidate','bones':result,'fingers':fingers,
            'missing_optional':sorted(set(BODY)-set(result)),
            'unmapped_channels':(['fingers'] if not fingers['complete'] else [])+['face','gaze','camera'],
            'support':'body_and_complete_finger_chains_development_candidate'}

def exact_cmu_mapping(rig):
    """Exact CMU 31-joint sample mapping, validated against real parents.

    No name fuzzy matching is used.  ``chest`` is ``Spine1``; the Mixamo
    ``spine1`` semantic is intentionally absent so one source bone cannot be
    consumed by two target semantics.
    """
    armature(rig)
    for name in CMU_BONES.values():
        require(name in rig.data.bones,'CMU源骨缺失：'+name)
    anchors=set(CMU_BONES.values())|{'LHipJoint','RHipJoint','LowerBack','Neck1'}
    missing=[name for name in sorted(anchors) if name not in rig.data.bones]
    require(not missing,'CMU层级锚点缺失：'+', '.join(missing))
    for name,parent in CMU_EXPECTED_PARENTS.items():
        actual=rig.data.bones[name].parent
        require((actual.name if actual else None)==parent,
                'CMU层级不符：%s 父级 %r，期望 %r'%(name,actual.name if actual else None,parent))
    values=list(CMU_BONES.values())
    require(len(values)==len(set(values)),'CMU精确映射复用了源骨')
    return {'schema':'c94.retarget.mapping.v1','preset':'cmu_exact_31_joint_sample_v1',
            'bones':dict(CMU_BONES),'fingers':{'complete':[],'skipped':{}},
            'missing_optional':['spine1'],'unmapped_channels':['fingers','gaze','camera'],
            'support':'body_development_candidate','validation':'exact names and exact parent links'}

_LIMB_NAMES = {
    "shoulder": ("clavicle", "collar", "shoulder"),
    "upper_arm": ("upperarm", "arm", "humerus"),
    "forearm": ("forearm", "lowerarm", "ulna"),
    "hand": ("hand", "wrist"),
    "thigh": ("thigh", "upleg", "upperleg", "femur"),
    "shin": ("shin", "calf", "leg", "lowerleg", "tibia"),
    "foot": ("foot", "ankle"),
    "toes": ("toes", "toe", "toebase", "ball"),
}
_TORSO_NAME = re.compile(r"(?:spine|chest|thorax|torso)[0-9]*")


def propose_fbx_mapping(rig):
    """Suggest body roles using known names and connected limb chains."""
    armature(rig)
    try:
        exact = mapping(rig)
    except ValueError:
        exact = None
    if exact is not None:
        hip = rig.data.bones[exact["bones"]["hips"]]
        root = hip.parent_recursive[-1] if hip.parent else hip
        return {"bones": exact["bones"], "root": root.name, "preset": exact["preset"],
                "missing": [], "ambiguous": {}}

    lookup = {}
    tokens = {}
    for bone in rig.data.bones:
        token = re.sub(r"[^a-z0-9]", "", bone.name.rsplit(":", 1)[-1].lower())
        tokens[bone.name] = token
        lookup.setdefault(token, []).append(bone)
    bones = {}
    ambiguous = {}

    def choose(semantic, names, ancestor=None):
        choices = sorted({bone.name for name in names for bone in lookup.get(name, ())
                          if ancestor is None or ancestor in bone.parent_recursive})
        if len(choices) == 1:
            bones[semantic] = choices[0]
            return rig.data.bones[choices[0]]
        if choices:
            ambiguous[semantic] = choices
        return None

    hips = choose("hips", ("hips", "hip", "pelvis"))
    head = choose("head", ("head", "head01", "skull"), hips)
    for side, word in (("L", "left"), ("R", "right")):
        for chain in (("shoulder", "upper_arm", "forearm", "hand"),
                      ("thigh", "shin", "foot", "toes")):
            ancestor = hips
            for role in chain:
                names = {form for alias in _LIMB_NAMES[role] for marker in (side.lower(), word)
                         for form in (marker + alias, alias + marker)}
                match = choose(role + "." + side, names, ancestor)
                if match is not None:
                    ancestor = match

    arms = [rig.data.bones.get(bones.get("upper_arm." + side, "")) for side in ("L", "R")]
    if hips is not None and head is not None and all(arm is not None for arm in arms):
        # The thoracic link is the nearest named torso ancestor of the head
        # that also carries both arms. Helper branches stay out of this path.
        arm_chains = [set(bone.parent_recursive) for bone in arms]
        for candidate in head.parent_recursive:
            if (_TORSO_NAME.fullmatch(tokens[candidate.name])
                    and hips in candidate.parent_recursive
                    and all(candidate in chain for chain in arm_chains)):
                bones["chest"] = candidate.name
                spine = [joint.name for joint in candidate.parent_recursive
                         if hips in joint.parent_recursive and _TORSO_NAME.fullmatch(tokens[joint.name])]
                if spine:
                    bones["spine"] = spine[-1]
                if len(spine) == 2:
                    bones["spine1"] = spine[0]
                elif len(spine) > 2:
                    ambiguous["spine1"] = list(reversed(spine[:-1]))
                choose("neck", ("neck", "neck1", "neck01", "cervical"), candidate)
                break

    root = hips.parent_recursive[-1].name if hips is not None and hips.parent else bones.get("hips")
    return {"bones": bones, "root": root, "preset": "generic_fbx_profile",
            "missing": sorted(REQUIRED - bones.keys()), "ambiguous": ambiguous}


def active_clip(rig,source_fps=None):
    armature(rig)
    ad=rig.animation_data
    require(ad is not None and ad.action is not None and ad.action_slot is not None,
            '请显式选择源骨架的Action和Slot')
    require(not ad.use_tweak_mode,'请退出源骨架的NLA Tweak模式')
    action,slot=ad.action,ad.action_slot
    require(slot in action.slots[:] and slot.target_id_type=='OBJECT','源Action Slot身份无效')
    times=[]
    for layer in action.layers:
        for strip in layer.strips:
            require(strip.type=='KEYFRAME','当前源Action含未验证条带类型')
            bag=strip.channelbag(slot)
            if bag:
                times.extend(float(k.co.x) for f in bag.fcurves for k in f.keyframe_points)
                times.extend(float(k.co.x) for f in bag.fcurves for k in f.sampled_points)
    require(times and min(times)<max(times),'所选源Slot没有有效动画片段')
    if source_fps is None:
        try:
            timebase=json.loads(action.get(TIME_KEY,''))
            require(slot.identifier in timebase['slots'],'当前Slot没有已记录时基；请明确源fps')
            source_fps=timebase['fps']
        except (ValueError,KeyError,TypeError) as exc:
            raise ValueError('当前Action/Slot没有可信时基记录；请明确源fps') from exc
    require(type(source_fps) in (int,float) and 0<source_fps<=1000,'源fps必须明确且有效')
    return {'action':action,'slot':slot,'fps':float(source_fps),'range':(min(times),max(times)),
            'evaluation':'selected_action_no_nla','nla_preserved':True}

def _is_alternate_armature_slot(rig, action, slot):
    if slot.target_id_type != 'OBJECT' or any(owner != rig for owner in slot.users()):
        return False
    paths = set()
    for layer in action.layers:
        for strip in layer.strips:
            if strip.type == 'KEYFRAME':
                bag = strip.channelbag(slot)
                if bag:
                    paths.update(curve.data_path for curve in bag.fcurves)
    if not any(path.startswith('pose.bones[') for path in paths):
        return False
    try:
        for path in paths:
            rig.path_resolve(path)
    except ValueError:
        return False
    return True


def _mark_fbx_take_timebase(rig, imported_actions, clip):
    """Apply the file-global rate only to confirmed slots of new Actions."""
    active_action, active_slot = clip['action'], clip['slot']
    require(active_action in imported_actions, '当前源Action不是本次FBX导入新建；请明确源fps')
    for action in imported_actions:
        if action == active_action:
            slot = active_slot
        else:
            if TIME_KEY in action:
                continue
            slot = next((candidate for candidate in action.slots
                         if candidate.identifier == active_slot.identifier
                         and candidate.name_display == rig.name), None)
            if slot is None or not _is_alternate_armature_slot(rig, action, slot):
                continue
        action[TIME_KEY] = json.dumps({'fps':clip['fps'],'slots':[slot.identifier]}, ensure_ascii=False)


def import_fbx(filepath,*,provenance=None):
    """Keep imported IDs; restore scene fps/range/camera and selection."""
    path=Path(filepath).resolve(strict=True)
    require(path.suffix.lower()=='.fbx','此入口当前仅接FBX；其他来源尚未晋升')
    scene=bpy.context.scene
    require(bpy.context.mode=='OBJECT','导入前请切到物体模式')
    old=(scene.render.fps,scene.render.fps_base,scene.frame_start,scene.frame_end,
         scene.frame_current,scene.frame_subframe,scene.camera)
    selected=list(bpy.context.selected_objects); active=bpy.context.view_layer.objects.active
    before=set(bpy.data.objects); before_actions=set(bpy.data.actions); imported=[]
    try:
        require(hasattr(bpy.ops.wm.fbx_import,'get_rna_type'),'当前版本未暴露原生FBX导入器')
        status=bpy.ops.wm.fbx_import(filepath=str(path),use_anim=True)
        fps=scene.render.fps/scene.render.fps_base
        imported=[o for o in bpy.data.objects if o not in before]
        require('FINISHED' in status,'FBX导入器未完成；新增数据保留供诊断')
        rigs=[o for o in imported if o.type=='ARMATURE']
        require(len(rigs)==1,'当前首片入口要求文件含一个源骨架；请在场景中明确选择多骨架来源')
        rig=rigs[0]
        record={'schema':'c94.retarget.source.v1','file':str(path),'format':'FBX','fps':fps,
                'objects':[o.name for o in imported],'provenance':provenance or {},
                'importer':'wm.fbx_import','scene_settings_preserved':True}
        rig[SOURCE_KEY]=json.dumps(record,ensure_ascii=False)
        clip=active_clip(rig,fps)
        _mark_fbx_take_timebase(rig, set(bpy.data.actions)-before_actions, clip)
        return {'rig':rig,'objects':imported,'clip':clip,'record':record}
    except Exception as exc:
        # A native importer may invoke foreign handlers. An ID difference is
        # not proof of ownership: keep partial Objects rather than delete them.
        partial=[o.name for o in bpy.data.objects if o not in before]
        detail='；新增对象保留供诊断：'+', '.join(partial[:8]) if partial else ''
        raise ValueError(str(exc)+detail) from exc
    finally:
        scene.render.fps,scene.render.fps_base=old[:2]
        scene.frame_start,scene.frame_end=old[2:4]
        scene.camera=old[6]
        scene.frame_set(old[4],subframe=old[5])
        for o in bpy.context.selected_objects: o.select_set(False)
        for o in selected:
            if o.name in bpy.context.view_layer.objects: o.select_set(True)
        bpy.context.view_layer.objects.active=active
