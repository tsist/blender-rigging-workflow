"""Finite PHYS-only time study; static solver and adapter remain frozen."""
import json,time,traceback
from pathlib import Path
import bpy,numpy as np
import baked_observer as Obs,character_state as C
from native_adapter import ChainPreview
from evidence import TypedWriter,checked,dump


def run(config,job_directory):
    job=Path(job_directory);writer=TypedWriter();preview=None;before=None
    report={'schema':'hrig.chain-temporal-native.v1','status':'running','samples':[],
            'baseline_checks':[],'processed_times':[]}
    def observe(t,phase):
        state=Obs.capture(bpy,observer,t,lambda:{'status':'pass'})
        report['samples'].append({'time':t,'phase':phase,'state':writer.pack(state)})
        return state
    def baseline_exact(state,index,archive,mapping,path=''):
        if isinstance(mapping,str):
            expected=archive[mapping][index];actual=np.asarray(state)
            same=actual.dtype==expected.dtype and actual.shape==expected.shape and actual.tobytes()==expected.tobytes()
            if not same:raise ValueError('actual_baseline_drift:'+path)
            return 1
        return sum(baseline_exact(state[k],index,archive,v,path+'/'+k) for k,v in mapping.items())
    try:
        for resource in config['resources']:checked(resource)
        base=json.loads(checked(config['baseline']).read_bytes())
        offline=json.loads(checked(config['offline']).read_bytes())
        contract=json.loads(checked(config['native_contract']).read_bytes())
        assert offline['status']=='pass_sampled_source_geometry'
        assert [r['time'] for r in offline['rows']]==base['times']
        assert tuple(bpy.app.version)==(5,2,1) and bpy.app.build_hash.decode()=='9e2066aef7ef' and bpy.data.filepath==''
        bpy.ops.wm.open_mainfile(filepath=str(checked(config['input'])),use_scripts=False,load_ui=False)
        bpy.context.window.scene=bpy.data.scenes['Scene'];bpy.context.window.view_layer=bpy.context.scene.view_layers['ViewLayer']
        bpy.context.scene.frame_set(0);bpy.context.view_layer.update()
        before=C.capture();report['initial_canonical']=C.digest(before);dump(job/'output-author-before.json',before)
        rig=bpy.data.objects['HRIG_rear_hair'];preview=ChainPreview(rig)
        observer=Obs.freeze(bpy,('HRIG_front_hair','HRIG_rear_hair'));report['observer_contract']=observer
        assert observer==base['observer_contract']
        observe(0,'initial0');clock=time.monotonic()
        with np.load(checked(config['baseline_data']),allow_pickle=False) as source_archive:
            archive={k:source_archive[k] for k in source_archive.files}
            for index,row in enumerate(offline['rows']):
                if time.monotonic()-clock>contract['budget']['native_sampling_wall_seconds']:
                    raise TimeoutError('finite_native_sampling_budget')
                t=row['time'];candidate=row['result'];assert candidate['qualified']
                baseline=Obs.capture(bpy,observer,t,lambda:{'status':'pass'})
                count=sum(baseline_exact(baseline[k],index,archive,base['array_map'][k],k)
                          for k in ('rigs','colliders','proxies'))
                report['baseline_checks'].append({'time':t,'arrays':count,'exact':True})
                if candidate['status']=='candidate':preview.apply(candidate['joints'])
                else:assert candidate['status']=='original_certified'
                preview.physics_outputs(True);observe(t,'PHYS8')
                preview.restore();observe(t,'restored')
                report['processed_times'].append(t)
        observe(0,'final0');report['sampling_wall_seconds']=time.monotonic()-clock
        report['status']='pass_native_execution_only'
    except Exception as error:
        report['status']='native_error';report['error']={'message':str(error),'traceback':traceback.format_exc()}
    finally:
        try:
            if preview:preview.restore()
            if before is not None:
                bpy.context.scene.frame_set(0);bpy.context.view_layer.update()
                after=C.capture();dump(job/'output-author-after.json',after)
                report['final_author_exact']=after==before
                if not report['final_author_exact']:report['status']='native_error'
        except Exception as error:
            report['cleanup_error']={'message':str(error),'traceback':traceback.format_exc()};report['status']='native_error'
        for resource in config['resources']:checked(resource)
        report['resources_exact']=True
    writer.write(job/'output-geometry.json');dump(job/'output-chain.json',report)
    return {'status':report['status'],'samples':len(report['samples']),'processed':len(report['processed_times']),'error':report.get('error')}
