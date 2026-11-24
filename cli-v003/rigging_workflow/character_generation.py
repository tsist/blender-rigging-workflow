"""One composition boundary for native C94 generation and planned new chains."""
import json
import bpy
from . import generation, module_configuration, positioning


def generate(scene, reference, template_file, label='Character', *, configuration=None, chain_record=None, owned_ids=None):
    if owned_ids is not None and not isinstance(owned_ids, list):
        raise ValueError('owned_ids须为调用者的可写list或None')
    owned = [] if owned_ids is None else owned_ids
    from . import accessory_chains
    if (chain_record is None and isinstance(reference,bpy.types.Object)
            and accessory_chains.KEY in reference):
        raise ValueError('该完整参考含已规划附属链；请显式传入对应chain_record，不能静默省略')
    if chain_record is not None:
        chain_record = accessory_chains.validate_reference(scene, reference, chain_record)
        # The chain extension is a separate technical candidate. Its copied
        # base must still pass the original positioning/marker synchronization.
        state = positioning.read_state(scene)
        if state and state.get('fitted_reference'):
            base = scene.objects.get(chain_record['base']['object'])
            if base is None:
                raise ValueError('原定位参考已丢失，不能使用附属参考继续生成')
            if state['fitted_reference'] != base.name:
                raise ValueError('附属方案的基底与当前定位参考不对应，请先切回其定位状态')
            generation.validate_input(scene, base)
    before = module_configuration._ids()
    active, selected = bpy.context.view_layer.objects.active, list(bpy.context.selected_objects)
    try:
        result = generation.generate(scene, reference, template_file, label=label, configuration=configuration, owned_ids=owned)
        if chain_record is not None:
            from . import accessory_chains
            metadata=json.loads(result['rig'][generation.RIG_KEY])
            metadata['core_deforms']=list(metadata['deforms'])
            result['rig'][generation.RIG_KEY]=json.dumps(metadata,ensure_ascii=False,allow_nan=False)
            result['report']['chain_extensions'] = accessory_chains.install(result['rig'], reference, chain_record, owned_ids=owned)
        return result
    except Exception:
        module_configuration._cleanup(before, active, selected, owned_ids=owned)
        raise
