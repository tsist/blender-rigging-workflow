# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit, short-lived FK/IK pose pair for the inspected self-contained rig.

This preserves an already computed pair when no inputs changed. It does not
make the legacy IK solver exact. A fresh edited pose always uses native snap.
No state is global, serialized or trusted after reload/configuration edits.
"""
import json
import math
import bpy
from mathutils import Matrix,Vector


def refine_base_direction(rig, stem, side, target_matrix):
    """Refine native non-pole swing using stable squared direction distance."""
    name=stem+'_ik.'+side
    ctrl=rig.pose.bones[name]
    if ctrl.rotation_mode!='ZXY':
        raise ValueError('Expected inspected ZXY native IK base control')
    axis=target_matrix.to_3x3().col[1].normalized()
    center=float(ctrl.rotation_euler.y)
    def score(angle):
        ctrl.rotation_euler.y=angle
        rig.update_tag();bpy.context.view_layer.update()
        ev=rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
        return (ev.pose.bones[name].vector.normalized()-axis).length_squared
    left,right=center-math.pi/180,center+math.pi/180
    best=(score(center),center)
    for _ in range(32):
        a=left+(right-left)/3;b=right-(right-left)/3
        fa,fb=score(a),score(b);best=min(best,(fa,a),(fb,b))
        if fa>fb:left=a
        else:right=b
        if right-left<1e-7:break
    score(best[1])
    return {'before_y':center,'after_y':float(ctrl.rotation_euler.y),'axis_chord_squared':best[0]}


class PoseMatchPair:
    """A transaction-scoped pair; create again after mode/file/context changes.

    Only unanimated, self-contained, inspected Rigify arm/leg layouts qualify.
    Timeline, config or any pose edits invalidate reuse. A caller must close or
    discard this instance when its editing operation finishes.
    """
    def __init__(self,rig,limb,side,qc,matcher):
        if limb not in {'arm','leg'} or side not in {'L','R'}:
            raise ValueError('Explicit arm/leg and L/R required')
        objects,scenes=qc._dependency_objects(rig)
        if objects!={rig} or scenes:
            raise ValueError('Pose pairing only supports the inspected self-contained rig')
        self.rig,self.limb,self.side,self.qc,self.matcher=rig,limb,side,qc,matcher
        self.stem,self.lower,self.end=('upper_arm','forearm','hand') if limb=='arm' else ('thigh','shin','foot')
        self.prop=self.stem+'_parent.'+side
        self.fk=[self.stem+'_fk.'+side,self.lower+'_fk.'+side,self.end+'_fk.'+side]+([] if limb=='arm' else ['toe_fk.'+side])
        self.ik=[self.stem+'_ik.'+side,'MCH-'+self.lower+'_ik.'+side,'MCH-'+self.stem+'_ik_target.'+side]
        self.ctrl=[self.stem+'_ik.'+side,self.stem+'_ik_target.'+side,self.end+'_ik.'+side]
        self.tail=[] if limb=='arm' else ['toe_ik.'+side]
        self.extra=[] if limb=='arm' else ['foot_heel_ik.'+side,'foot_spin_ik.'+side]
        self.pair=None;self.closed=False

    def close(self):
        self.pair=None;self.closed=True

    def _context(self):
        rig=self.rig;scene=bpy.context.scene;ad=rig.animation_data
        if self.closed or rig!=bpy.context.view_layer.objects.active or rig.mode!='POSE':
            raise ValueError('An open pair on the active Pose armature is required')
        data_ad=rig.data.animation_data
        if ad and (ad.action or ad.nla_tracks) or data_ad and (data_ad.action or data_ad.nla_tracks):
            raise ValueError('Detach Actions/NLA; key captured results explicitly after matching')
        if rig.pose.bones[self.prop]['pole_vector']:
            raise ValueError('This bounded pair supports the inspected non-pole mode only')
        return {'frame':scene.frame_current,'subframe':scene.frame_subframe,
                'world':self.qc._plain(rig.matrix_world),'rig_data':rig.data.as_pointer(),
                'parent':rig.parent.as_pointer() if rig.parent else None,
                'parent_type':rig.parent_type,'parent_bone':rig.parent_bone,
                'solver':rig.pose.ik_solver,'pose_position':rig.data.pose_position,
                'parent_inverse':self.qc._plain(rig.matrix_parent_inverse),
                'config':self.qc.digest({'bones':[
                    {'name':b.name,'parent':b.parent.name if b.parent else None,
                     'rest':self.qc._plain(b.matrix_local),'head':list(b.head_local),'tail':list(b.tail_local),
                     'connected':b.use_connect,'deform':b.use_deform,'segments':b.bbone_segments,
                     'inherit':b.inherit_scale,
                     'inherit_rotation':b.use_inherit_rotation,
                     'constraints':self.qc._constraints(rig.pose.bones[b.name]),
                     'locks':[list(rig.pose.bones[b.name].lock_location),list(rig.pose.bones[b.name].lock_rotation),list(rig.pose.bones[b.name].lock_scale)]}
                    for b in rig.data.bones],
                    'object_constraints':self.qc._constraints(rig),'drivers':self.qc._drivers(rig),
                    'data_drivers':self.qc._drivers(rig.data)})}

    @staticmethod
    def _values(state):
        return [(owner.as_pointer(),channels,custom) for owner,channels,custom in state]

    def _evaluated(self):
        bpy.context.view_layer.update()
        ev=self.rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
        return [(p.name,self.qc._plain(ev.matrix_world@p.matrix),
                 [self.qc._plain(ev.matrix_world@p.matrix@p.bbone_segment_matrix(i,rest=False))
                  for i in range(p.bone.bbone_segments+1)] if p.bone.bbone_segments>1 else [])
                for p in ev.pose.bones]

    def _residual(self,a,b):
        aa={n:Matrix(m) for n,m,_ in a};bb={n:Matrix(m) for n,m,_ in b}
        segments_a={n:segments for n,_,segments in a};segments_b={n:segments for n,_,segments in b}
        if (len(segments_a)!=len(a) or len(segments_b)!=len(b)
                or segments_a.keys()!=segments_b.keys()):
            raise ValueError('B-Bone residual requires matching complete bone snapshots')
        maximum_bbone_segment_delta=0.
        for name,segments in segments_a.items():
            other=segments_b[name]
            if len(segments)!=len(other):
                raise ValueError('B-Bone segment count changed: '+name)
            for index in range(len(segments)):
                left,right=segments[index],other[index]
                if (len(left)!=4 or len(right)!=4
                        or any(len(row)!=4 for row in left)
                        or any(len(row)!=4 for row in right)):
                    raise ValueError('B-Bone segment matrix shape changed: '+name)
                maximum_bbone_segment_delta=max(maximum_bbone_segment_delta,
                    max(abs(left[row][column]-right[row][column])
                        for row in range(4) for column in range(4)))
        names=['DEF-'+stem+'.'+self.side for stem in
               ([self.stem,self.lower,self.end] if self.limb=='arm' else [self.stem,self.lower,self.end,'toe'])]
        return {'maximum_matrix_delta':max(abs(x-y) for n in names for ra,rb in zip(aa[n],bb[n]) for x,y in zip(ra,rb)),
                'maximum_endpoint_distance':max((aa[n]@v-bb[n]@v).length for n in names
                    for v in (Vector((0,0,0)),Vector((0,self.rig.data.bones[n].length,0)))),
                'maximum_bbone_segment_delta':maximum_bbone_segment_delta}

    def _rollback(self,state,evaluated):
        self.pair=None;self.qc._restore_runtime(state);self.rig.update_tag();bpy.context.view_layer.update()
        if self._values(self.qc._runtime_state(self.rig))!=self._values(state) or self._evaluated()!=evaluated:
            raise RuntimeError('Pose transaction rollback failed validation; inspect the unsaved work copy')

    def switch(self):
        rig=self.rig
        try:context=self._context()
        except Exception:
            self.pair=None
            raise
        before=self.qc._runtime_state(rig)
        evaluated_before=self._evaluated()
        mode=float(rig.pose.bones[self.prop]['IK_FK'])
        if mode not in {0.,1.}:raise ValueError('A complete FK or IK source is required')
        if self.pair and self.pair['contexts'].get(mode)==context:
            src=self.pair['states'].get(mode)
            if src and self._values(src)==self._values(before) and self.pair['evaluated'][mode]==evaluated_before:
                target=self.pair['states'][1.-mode]
                changed=[row for row,old in zip(target,before) if row[1:]!=old[1:]]
                scene=bpy.context.scene;autokey=scene.tool_settings.use_keyframe_insert_auto
                scene.tool_settings.use_keyframe_insert_auto=False
                try:
                    self.qc._restore_runtime(changed);rig.update_tag();bpy.context.view_layer.update()
                    if self._values(self.qc._runtime_state(rig))!=self._values(target):
                        raise ValueError('Paired restoration did not preserve every input')
                    if self._evaluated()!=self.pair['evaluated'][1.-mode]:
                        raise ValueError('Paired restoration changed evaluated skeleton output')
                except Exception:
                    self._rollback(before,evaluated_before)
                    raise
                finally:scene.tool_settings.use_keyframe_insert_auto=autokey
                return {'path':'paired_restore','changed_controls':[o.name for o,_,_ in changed],
                        'native_snap_rerun':False,'source_mode':mode,'target_mode':1.-mode,
                        'first_match_residual':self.pair['first_match_residual']}
        invalidated=self.pair is not None;self.pair=None
        scene=bpy.context.scene;autokey=scene.tool_settings.use_keyframe_insert_auto
        scene.tool_settings.use_keyframe_insert_auto=False
        try:
            objects,scenes=self.qc._dependency_objects(rig)
            if objects!={rig} or scenes:
                raise ValueError('New external dependencies invalidate this bounded pairing context')
            if mode==1.:
                bpy.context.view_layer.update()
                ev=rig.evaluated_get(bpy.context.evaluated_depsgraph_get())
                target_world=ev.matrix_world@ev.pose.bones['DEF-'+self.lower+'.'+self.side].matrix
                first=rig.pose.bones[self.fk[0]].matrix.copy()
                result=self.matcher.snap_fk_to_ik_preserving_roll(rig,self.limb,self.side)
                result['refinement']=refine_base_direction(rig,self.stem,self.side,first)
                result['roll']=self.matcher.preserve_distal_roll(rig,'DEF-'+self.lower+'.'+self.side,self.lower+'_tweak.'+self.side,target_world)
            else:
                result=getattr(bpy.ops.pose,'rigify_generic_snap_'+rig.data['rig_id'])(
                    input_bones=json.dumps(self.ik+self.tail),output_bones=json.dumps(self.fk),
                    ctrl_bones=json.dumps(self.ctrl+self.tail+self.extra))
                if 'FINISHED' not in result:raise ValueError('Native reverse snap failed')
                rig.pose.bones[self.prop]['IK_FK']=1.
                rig.update_tag();bpy.context.view_layer.update()
                result={'native_operator':'rigify_generic_snap'}
            after=self.qc._runtime_state(rig)
            evaluated_after=self._evaluated();residual=self._residual(evaluated_before,evaluated_after)
            self.pair={'contexts':{mode:context,1.-mode:self._context()},'states':{mode:before,1.-mode:after},
                       'evaluated':{mode:evaluated_before,1.-mode:evaluated_after},'first_match_residual':residual}
            return {'path':'native_refined' if mode==1. else 'native_reverse','source_mode':mode,
                    'target_mode':1.-mode,'invalidated':invalidated,'details':result,
                    'changed_controls':[a[0].name for a,b in zip(after,before) if a[1:]!=b[1:]],
                    'native_snap_rerun':True,'first_match_residual':residual}
        except Exception:
            self._rollback(before,evaluated_before)
            raise
        finally:scene.tool_settings.use_keyframe_insert_auto=autokey
