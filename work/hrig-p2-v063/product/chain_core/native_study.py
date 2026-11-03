"""One reversible source/output paired study; no UI, save or solver inside."""
import json,traceback
from pathlib import Path
import bpy,numpy as np
import baked_observer as Obs,character_state as C
from native_adapter import ChainPreview
from evidence import TypedWriter,checked,dump

def run(config,job_directory):
    job=Path(job_directory);writer=TypedWriter();preview=None;before=None
    report={'schema':'hrig.chain-representative-native.v1','status':'running','samples':[]}
    def observe(time,phase):
        state=Obs.capture(bpy,observer,time,lambda:{'status':'pass'})
        report['samples'].append({'time':time,'phase':phase,'state':writer.pack(state)})
        return state
    try:
        for resource in config['resources']:checked(resource)
        inputs=json.loads(checked(config['chain_input']).read_bytes())
        offline=json.loads(checked(config['offline']).read_bytes())
        gate=json.loads(checked(config['a4_gate']).read_bytes())
        assert gate['status']=='pass_fixed32_in_scope'
        assert offline['status']=='pass_in_scope'
        assert tuple(bpy.app.version)==(5,2,1) and bpy.app.build_hash.decode()=='9e2066aef7ef' and bpy.data.filepath==''
        bpy.ops.wm.open_mainfile(filepath=str(checked(config['input'])),use_scripts=False,load_ui=False)
        bpy.context.window.scene=bpy.data.scenes['Scene'];bpy.context.window.view_layer=bpy.context.scene.view_layers['ViewLayer']
        bpy.context.scene.frame_set(0);bpy.context.view_layer.update()
        before=C.capture();report['initial_canonical']=C.digest(before);dump(job/'output-author-before.json',before)
        rig=bpy.data.objects['HRIG_rear_hair'];preview=ChainPreview(rig)
        observer=Obs.freeze(bpy,('HRIG_front_hair','HRIG_rear_hair'));report['observer_contract']=observer
        ix={n:i for i,n in enumerate(observer['order']['HRIG_rear_hair'])}
        observe(0,'initial0')
        for row in offline['rows']:
            t=row['time'];candidate=row['result']
            if candidate['status']=='original_certified':continue
            assert candidate['qualified'] and candidate['status']=='candidate'
            pose=next(p for p in inputs['poses'] if p['time']==t)
            observe(t,'baseline');preview.physics_outputs(True);state=observe(t,'baseline_PHYS8')
            ht=state['rigs']['HRIG_rear_hair']['head_tail'][[ix[n] for n in inputs['physical']]].astype(float)
            assert np.array_equal(ht,np.asarray(pose['physical_head_tail']))
            preview.physics_outputs(False);preview.apply(candidate['joints'])
            observe(t,'ON_CONTROL');preview.physics_outputs(True);observe(t,'ON_PHYS8')
            preview.restore();observe(t,'restored')
        observe(0,'final0');report['status']='pass_native_execution_only'
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
    return {'status':report['status'],'samples':len(report['samples']),'error':report.get('error')}
