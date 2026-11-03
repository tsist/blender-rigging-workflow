"""Pinned weight observation/patch work units and independent native cold read."""
import copy
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import struct
import sys

sys.dont_write_bytecode=True
import bpy

ROOT=Path(__file__).resolve().parents[1]
OBSERVER=ROOT.parents[1]/'tools/blenderctl'


def sha(file):
    value=hashlib.sha256()
    with Path(file).open('rb') as stream:
        for data in iter(lambda:stream.read(1048576),b''):value.update(data)
    return value.hexdigest()


def checked(item):
    if set(item)!={'file','expected_sha256'} or not Path(item['file']).is_absolute() or sha(item['file'])!=item['expected_sha256']:
        raise ValueError('File role or SHA conflict: '+str(item))
    return Path(item['file']).resolve(strict=True)


def read(item):
    def finite_float(raw):
        value=float(raw)
        if not math.isfinite(value):raise ValueError('Nonfinite JSON number')
        return value
    def pairs(values):
        result={}
        for key,value in values:
            if key in result:raise ValueError('Duplicate JSON key')
            result[key]=value
        return result
    return json.loads(checked(item).read_text(encoding='utf-8-sig'),object_pairs_hook=pairs,parse_float=finite_float,
        parse_constant=lambda _:(_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def dump(file,value):
    raw=json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)
    with Path(file).open('x',encoding='utf-8') as stream:stream.write(raw)


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def select_context(value,frames=False):
    scene=bpy.data.scenes.get(value['scene'])
    if scene is None or scene.view_layers.get(value['view_layer']) is None:
        raise ValueError('Explicit Scene/View Layer missing')
    bpy.context.window.scene=scene
    bpy.context.window.view_layer=scene.view_layers[value['view_layer']]
    if frames and (scene.frame_current!=value['frame'] or abs(scene.frame_subframe-value['subframe'])>1e-7):
        raise ValueError('SAVED_DISK_V1 frame/subframe baseline conflict')


def observed_number(value):
    value=float(value)
    if math.isfinite(value):return value
    return {'non_finite':'nan' if math.isnan(value) else 'inf' if value>0 else '-inf'}


def raw_rows(obj):
    names={g.index:g.name for g in obj.vertex_groups}
    return [{names[g.group]:observed_number(g.weight) for g in vertex.groups} for vertex in obj.data.vertices]


def runtime_extra_users():
    import inspection
    return {inspection.key(inspection.identity(item)):item.use_extra_user for item in inspection.all_ids()}


def automatic_texspace():
    return {mesh.name:{'location':[observed_number(x) for x in mesh.texspace_location],
                       'size':[observed_number(x) for x in mesh.texspace_size]}
            for mesh in bpy.data.meshes if mesh.use_auto_texspace}


def native_sample(obj):
    obj.update_tag(refresh={'DATA'})
    bpy.context.view_layer.update()
    graph=bpy.context.evaluated_depsgraph_get()
    ev=graph.id_eval_get(obj)
    if not ev.is_evaluated:raise ValueError('Native evaluated Mesh Object required')
    mesh=ev.to_mesh()
    try:
        value=hashlib.sha256()
        preview=[];invalid=0;examples=[]
        for vertex in mesh.vertices:
            point=ev.matrix_world@vertex.co
            if any(not math.isfinite(x) for x in point):
                invalid+=1
                if len(examples)<16:examples.append({'index':vertex.index,'world':[observed_number(x) for x in point]})
            value.update(struct.pack('<3f',*point))
            if len(preview)<16:preview.append([observed_number(x) for x in point])
        return {'vertices':len(mesh.vertices),'world_f32_sha256':value.hexdigest(),'preview_world':preview,
                'finite':invalid==0,'nonfinite_vertices':invalid,'nonfinite_examples':examples,
                'scope':'current evaluated geometry only; no human quality or continuous motion claim'}
    finally:ev.to_mesh_clear()


def paths():
    import inspection
    origins={}
    def add(raw,kind,owner,library=None):
        if not raw or raw=='<builtin>':return
        target=os.path.normcase(os.path.normpath(bpy.path.abspath(raw,library=library)))
        origins.setdefault(target,[]).append({'kind':kind,'owner':inspection.identity(owner)})
    for item in inspection.all_ids():
        weak=getattr(item,'library_weak_reference',None)
        if weak is not None:add(weak.filepath,'weak_library_reference',item)
        raw=getattr(item,'filepath',None)
        if isinstance(raw,str):add(raw,'native_ID_filepath',item,item.library)
    result=[]
    for raw in sorted(set(bpy.utils.blend_paths(absolute=True,packed=False,local=False))):
        path=Path(os.path.normcase(os.path.normpath(raw)))
        if not str(path):continue
        provenance=sorted(origins.get(str(path),[]),key=lambda x:json.dumps(x,sort_keys=True))
        weak_only=bool(provenance) and all(x['kind']=='weak_library_reference' for x in provenance)
        result.append({'target':str(path),'kind':'file' if path.is_file() else 'directory_unverified' if path.is_dir() else 'missing',
                       'sha256':sha(path) if path.is_file() else None,'typed_origins':provenance,
                       'missing_weak_blend_reference_only':weak_only and path.suffix.lower()=='.blend' and not path.exists()})
    return result


def guard_external(operation,observed):
    if set(operation)!={'schema','context','target','patch','external_inputs','inherited_missing_weak_references'} or operation['schema']!='hrig.weights.operation.v1':
        raise ValueError('Exact weight operation fields including external_inputs required')
    declared=operation['external_inputs']
    return validate_native_inputs(declared,operation['inherited_missing_weak_references'],observed)


def validate_native_inputs(declared,declared_missing,observed):
    """Exact pinned native file set, shared without impersonating an operation."""
    if type(declared) is not list:raise ValueError('external_inputs must be a descriptor list')
    actual={}
    inherited=[]
    for item in observed:
        if item['kind']!='file':
            if item.get('missing_weak_blend_reference_only'):
                inherited.append(item['target']);continue
            raise ValueError('Unverified native external path blocks patch: '+item['target'])
        path=str(Path(item['target']).resolve(strict=True)).casefold()
        actual[path]=item['sha256']
    expected={}
    for item in declared:
        path=str(checked(item)).casefold()
        if path in expected:raise ValueError('Duplicate native external input')
        expected[path]=item['expected_sha256']
    if actual!=expected:raise ValueError('Declared external_inputs differ from observed native static file targets/SHA')
    if type(declared_missing) is not list or len(declared_missing)>1024:raise ValueError('Explicit bounded inherited missing list required')
    canonical=[]
    for path in declared_missing:
        if type(path) is not str or not Path(path).is_absolute() or len(path)>4096 or Path(path).exists():
            raise ValueError('Inherited missing path conflict')
        canonical.append(str(Path(path).resolve()).casefold())
    if len(canonical)!=len(set(canonical)) or set(canonical)!={str(Path(p).resolve()).casefold() for p in inherited}:
        raise ValueError('Missing weak references require exact explicit inheritance; origins remain partial/unresolved')
    return {'status':'pinned_native_static_file_set','count':len(expected),
            'inherited_missing_weak_blend_references':inherited,
            'weak_origin_coverage':'partial native ID filepath and ID.library_weak_reference only; not proof of non-necessity',
            'not_certified':['dynamic or opaque add-on inputs','cache contents','render/resource portability']}


def check_rows(before,after,request,rig,obj):
    if len(before)!=len(after):raise ValueError('Raw vertex row count changed')
    deform={b.name for b in rig.data.bones if b.use_deform}
    locked={g.name for g in obj.vertex_groups if g.lock_weight}
    expected={r['index']:r for r in request['rows']}
    preserve_sum=request['schema']=='hrig.weights.patch.v2'
    if preserve_sum and request['sum_policy']!='preserve_source_deform_sum':
        raise ValueError('Independent source deform-sum policy conflict')
    for index,(first,second) in enumerate(zip(before,after)):
        if index not in expected:
            if first!=second:raise ValueError('Unselected weight row changed')
            continue
        row=expected[index]
        if first!=row['before']:raise ValueError('Independent selected before-row precondition failed')
        desired={n:w for n,w in first.items() if n not in deform}
        desired.update({n:struct.unpack('<f',struct.pack('<f',float(w)))[0] for n,w in row['after'].items()})
        if second!=desired:raise ValueError('Independent full selected-row oracle failed')
        if {n:w for n,w in first.items() if n in locked}!={n:w for n,w in second.items() if n in locked}:
            raise ValueError('Locked membership changed')
        if preserve_sum:
            expected_sum=row['expected_deform_sum']
            if type(expected_sum) not in (int,float) or not math.isfinite(expected_sum) or expected_sum<=0:
                raise ValueError('Independent expected_deform_sum must be finite and positive')
            source_sum=math.fsum(weight for name,weight in first.items() if name in deform)
            if source_sum!=expected_sum:
                raise ValueError('Independent source deform-sum precondition failed')
            requested_sum=math.fsum(row['after'].values())
            if requested_sum<=0 or abs(requested_sum-expected_sum)>1e-6:
                raise ValueError('Independent requested deform sum differs from source')
            stored_sum=math.fsum(weight for name,weight in second.items() if name in deform)
            if stored_sum<=0 or abs(stored_sum-expected_sum)>1e-6:
                raise ValueError('Independent stored deform sum differs from source')


def run(config,job):
    if tuple(bpy.app.version)!=(5,2,1) or bpy.context.preferences.filepaths.use_scripts_auto_execute:
        raise ValueError('Requires Blender5.2.1 with embedded autoexec disabled')
    if Path(config['addon_root']).resolve()!=ROOT/'addon' or Path(config['observer_root']).resolve()!=OBSERVER:
        raise ValueError('Worker implementation role root mismatch')
    required=set((ROOT/'addon/humanoid_rigging_probe').glob('*.py'))|{
        ROOT/'scripts/character_state.py',ROOT/'scripts/weight_fixture.py',OBSERVER/'inspection.py',OBSERVER/'protocol.py'}
    files=[checked(x) for x in config['implementation']]
    if len(files)!=len(set(files)) or set(files)!=required:raise ValueError('Exact implementation closure required')
    sys.path.insert(0,str(ROOT/'addon'))
    from humanoid_rigging_probe import weights,persistence
    import inspection
    if Path(inspection.__file__).resolve()!=OBSERVER/'inspection.py':raise ValueError('Observer import differs from pinned role')
    state=load('_hrig_character_state',ROOT/'scripts/character_state.py')
    fixture=load('_hrig_weight_fixture',ROOT/'scripts/weight_fixture.py')
    mode=config['mode']
    common={'mode','implementation','addon_root','observer_root'}
    extra={'inspect':{'input','target','context'},'patch':{'input','operation'},'fixture':{'case'},'cold':{'input','producer_report'}}
    expected=common|extra.get(mode,set())
    if mode=='cold' and 'operation' in config:expected.add('operation')
    if mode not in extra or set(config)!=expected:raise ValueError('Unknown worker mode/fields')
    report={'schema':'hrig.weights.report.v1','mode':mode,'blender':bpy.app.version_string,'build':bpy.app.build_hash.decode(),
        'implementation':config['implementation'],'entry_sha':sha(__file__),'GUI':'deferred_by_user','FPS':'not_measured',
        'storage_scope':'declared character semantic fields; UI, opaque add-on state, VSE and cache/render correctness not certified'}
    manifest=operation=None
    if mode=='cold':
        producer=read(config['producer_report'])
        if (producer.get('status')!='candidate_waiting_independent_cold' or producer['candidate']!=config['input']
                or producer['implementation']!=config['implementation'] or producer['entry_sha']!=sha(__file__)):
            raise ValueError('Cold producer/candidate/implementation pairing conflict')
        if producer.get('operation_descriptor')!=config.get('operation'):raise ValueError('Cold operation role differs')
        if 'operation' in config:
            operation=read(config['operation'])
            if operation!=producer['operation']:raise ValueError('Cold operation content differs')
        bpy.ops.wm.open_mainfile(filepath=str(checked(config['input'])),load_ui=False,use_scripts=False)
        select_context(producer['context'],frames=True)
        actual=state.summarize(state.capture())
        if actual!=producer['after_snapshot']:
            report.update(status='cold_mismatch',changes=state.diff(producer['after_snapshot'],actual))
            dump(job/'output-weights.json',report)
            raise ValueError('Independent character storage cold mismatch')
        if paths()!=producer['external_paths_after']:raise ValueError('External path targets/content changed')
        if operation:report['external_input_validation']=guard_external(operation,paths())
        target=producer['target'];obj=bpy.data.objects[target['object']]
        if raw_rows(obj)!=producer['after_raw_rows']:raise ValueError('Cold raw memberships differ')
        runtime=runtime_extra_users()
        original=producer['runtime_extra_users_after']
        report['runtime_extra_user_changes']=[{'id':json.loads(k),'before':original.get(k),'after':runtime.get(k)}
            for k in sorted(original.keys()|runtime.keys()) if original.get(k)!=runtime.get(k)]
        report['runtime_extra_users_observed']=runtime
        texspace=automatic_texspace()
        previous=producer['automatic_texspace_after']
        report['automatic_texspace_observed']=texspace
        report['automatic_texspace_changes']=[{'mesh':k,'before':previous.get(k),'after':texspace.get(k)}
            for k in sorted(previous.keys()|texspace.keys()) if previous.get(k)!=texspace.get(k)]
        report['automatic_texspace_scope']='automatic cached bounds observed separately; manual values, auto flags, geometry/keys remain strict; Generated/ORCO/material/render stability not certified'
        report['runtime_extra_user_scope']='internal runtime tags observed separately; users/fake/real references remain strict storage checks'
        if 'native_sample_after' in producer:
            sample=native_sample(obj)
            if not sample['finite'] or sample['world_f32_sha256']!=producer['native_sample_after']['world_f32_sha256']:
                raise ValueError('Independent current native evaluated geometry differs')
            report['native_sample']=sample
        manifest=producer.get('manifest')
        if manifest:
            current=fixture.capture(manifest)
            validation=fixture.validate(manifest,producer['fixture_before'],current,producer['result'])
            if not validation['ok']:raise ValueError('Cold independent fixture oracle failed: '+str(validation))
            report['fixture_validation']=validation
        checked(config['input'])
        report.update(status='pass',source_unchanged=True,target=target,after_snapshot=actual,
            operation_ok=producer['operation_ok'],raw_memberships_match=True,external_paths=paths())
    else:
        if mode=='fixture':
            bpy.ops.wm.read_factory_settings(use_empty=True)
            manifest=fixture.create_case(config['case'])
            target={'object':manifest['object'],'rig':manifest['rig']};ctx=manifest['context']
        else:
            bpy.ops.wm.open_mainfile(filepath=str(checked(config['input'])),load_ui=False,use_scripts=False)
            report['input_file_version']=list(bpy.data.version)
            if tuple(bpy.data.version[:2])>tuple(bpy.app.version[:2]):raise ValueError('Future source version rejected')
            if mode=='patch':
                operation=read(config['operation'])
                if set(operation)!={'schema','context','target','patch','external_inputs','inherited_missing_weak_references'} or operation['schema']!='hrig.weights.operation.v1':
                    raise ValueError('Exact weight operation fields required')
                target=operation['target'];ctx=operation['context']
                report.update(operation=operation,operation_descriptor=config['operation'])
            else:target,ctx=config['target'],config['context']
            select_context(ctx,frames=mode=='patch')
        obj=bpy.data.objects.get(target['object']);rig=bpy.data.objects.get(target['rig'])
        if obj is None or rig is None:raise ValueError('Explicit object/rig not found')
        ctx={**ctx,'frame':bpy.context.scene.frame_current,'subframe':bpy.context.scene.frame_subframe}
        before=state.capture();before_summary=state.summarize(before);external_before=paths()
        runtime_before=runtime_extra_users()
        report['runtime_extra_users_before']=runtime_before
        report['automatic_texspace_before']=automatic_texspace()
        if operation:report['native_sample_before']=native_sample(obj)
        if operation:report['external_input_validation']=guard_external(operation,external_before)
        raw_before=raw_rows(obj)
        inspection_result=weights.inspect_weights(bpy.context,obj,rig)
        report.update(input=config.get('input'),target=target,context=ctx,inspection=inspection_result,
                      before_snapshot=before_summary,external_paths_before=external_before)
        if mode=='inspect':
            if not inspection_result['ok']:raise ValueError('Weight inspection refused: '+str(inspection_result['error']))
            after=state.summarize(state.capture())
            if after!=before_summary:raise ValueError('Read-only inspection changed character storage')
            deform={b.name for b in rig.data.bones if b.use_deform}
            consumers={x['group'] for x in inspection_result['group_consumers']}
            locked={g.name for g in obj.vertex_groups if g.lock_weight}
            previews=[];normalized=[]
            for index,row in enumerate(raw_before):
                if any(type(w) not in (int,float) or not math.isfinite(w) or not 0<=w<=1 for w in row.values()):continue
                positive={n:w for n,w in row.items() if n in deform and w>0}
                if len(positive)>=2 and len(previews)<32:previews.append({'index':index,'raw':row,'deform':positive})
                if (len(positive)>=2 and len(normalized)<32 and abs(math.fsum(positive.values())-1)<=1e-6
                    and not (set(row)&deform&(consumers|locked))):
                    normalized.append({'index':index,'raw':row,'deform':positive})
            checked(config['input'])
            report.update(status='pass',source_unchanged=True,edit_candidates=previews,after_snapshot=after,
                          normalized_edit_candidates=normalized,
                          native_sample=native_sample(obj))
        else:
            if mode=='fixture':
                request=manifest['request']
                signature=inspection_result.get('snapshot',{})
                request['expected_topology_sha256']=signature.get('topology_sha256','0'*64)
                request['expected_groups_sha256']=signature.get('groups_sha256','0'*64)
                fixture_before=fixture.capture(manifest)
            else:
                if not inspection_result['ok']:raise ValueError('Weight patch domain refused: '+str(inspection_result['error']))
                request=operation['patch']
            runtime=copy.deepcopy(request)
            if mode=='fixture' and config['case']=='reject_nan':runtime['rows'][0]['after']['A']=float('nan')
            original=weights._set_row
            calls=0
            def injected(*args):
                nonlocal calls
                value=original(*args);calls+=1
                if calls==1:raise RuntimeError('fixture injected after first completed native weight row')
                return value
            if mode=='fixture' and config['case'] in ('rollback_injected','preserve_rollback_injected'):weights._set_row=injected
            try:result=weights.patch_weights(bpy.context,obj,rig,runtime)
            finally:weights._set_row=original
            report.update(result=result,operation_ok=result['ok'])
            after=state.capture();raw_after=raw_rows(obj)
            if mode=='fixture':
                fixture_after=fixture.capture(manifest)
                validation=fixture.validate(manifest,fixture_before,fixture_after,result)
                report.update(manifest=manifest,fixture_before=fixture_before,fixture_after=fixture_after,fixture_validation=validation)
                if not validation['ok']:
                    report['status']='fixture_failed';dump(job/'output-weights.json',report)
                    raise ValueError('Independent fixture validation failed: '+str(validation))
                # Pose exploration is in this owned synthetic rig only. Restore
                # pose0 before saving, then cold validates the exact same pose.
                poses=[fixture.set_pose(manifest,i) for i in (1,2,0)]
                if any(x['linear_skin_max_distance']>2e-5 for x in poses):raise ValueError('Independent pose skin oracle failed')
                report['pose_samples']=poses
                after=state.capture();raw_after=raw_rows(obj)
            elif not result['ok']:
                report['status']='rejected';dump(job/'output-weights.json',report)
                raise ValueError('Requested weight patch refused: '+str(result['error']))
            if result['ok']:
                indices=[r['index'] for r in request['rows']]
                first=state.protected(before,obj.name,obj.data.name,indices)
                second=state.protected(after,obj.name,obj.data.name,indices)
                if first!=second:
                    report.update(status='protection_failed',changes=state.diff(first,second));dump(job/'output-weights.json',report)
                    raise ValueError('Non-target character fields changed')
                check_rows(raw_before,raw_after,request,rig,obj)
            elif before!=after or raw_before!=raw_after:raise ValueError('Rejected fixture changed character storage')
            external_after=paths()
            if external_after!=external_before:raise ValueError('Operation changed external inputs')
            if operation:guard_external(operation,external_after)
            if operation:
                sample=native_sample(obj)
                if not sample['finite']:raise ValueError('Current native evaluated geometry is nonfinite')
                report['native_sample_after']=sample
            writer=persistence.save_candidate(job/'output-candidate.blend',job)
            after_write=state.summarize(state.capture())
            after_summary=state.summarize(after)
            if after_write!=after_summary:raise ValueError('Native writer changed in-memory character storage')
            runtime_after=runtime_extra_users()
            if runtime_after!=runtime_before:raise ValueError('Worker changed original runtime extra-user bookkeeping')
            if 'input' in config:checked(config['input'])
            report.update(status='candidate_waiting_independent_cold',candidate={k:writer[k] for k in ('file','expected_sha256')},
                writer=writer,after_snapshot=after_summary,after_raw_rows=raw_after,external_paths_after=external_after,
                runtime_extra_users_after=runtime_after,automatic_texspace_after=automatic_texspace())
    for item in config['implementation']:checked(item)
    if 'operation' in config:checked(config['operation'])
    if operation:guard_external(operation,paths())
    if sha(__file__)!=report['entry_sha']:raise ValueError('Worker entry changed')
    report['collection_active_index_runtime_observation']=state.collection_active_indices()
    report['collection_active_index_scope']='raw index observed separately; null active identity canonical index=-1, native new-empty0/cold-1; nonnull active identity/index remain strict'
    dump(job/'output-weights.json',report)
    return {'status':report['status'],'mode':mode,'operation_ok':report.get('operation_ok'),
            'source_unchanged':report.get('source_unchanged'),'GUI':'deferred','FPS':'not_measured'}


if __name__=='__main__':
    try:result=run(parameters,Path(job_directory))
    except Exception:
        import traceback
        (Path(job_directory)/'output-debug.txt').write_text(traceback.format_exc(),encoding='utf-8')
        raise
