"""Remove only proven duplicate curves and unresolved outputs on an owned copy."""
from collections import defaultdict


def signature(f):
    """Compare native writable evaluation fields, not just visible key values."""
    def rna_fields(value, excluded=()):
        result={}
        for prop in value.bl_rna.properties:
            if prop.identifier in excluded or prop.is_readonly or prop.type in {'POINTER','COLLECTION'}:
                continue
            item=getattr(value,prop.identifier)
            result[prop.identifier]=tuple(item) if getattr(prop,'is_array',False) else item
        return result
    d = f.driver
    if f.sampled_points:
        raise ValueError('Sampled driver curves require explicit review')
    modifiers=[]
    for m in f.modifiers:
        # Envelope control points have a second nested evaluation structure.
        if m.type=='ENVELOPE':
            raise ValueError('Envelope driver modifiers require explicit review')
        modifiers.append((m.type,rna_fields(m)))
    return {'curve':rna_fields(f,{'select','color','color_mode','lock'}),
            'driver':rna_fields(d),
            'variables':[(v.name,v.type,[(t.id.as_pointer() if t.id else None,rna_fields(t)) for t in v.targets]) for v in d.variables],
            'keys':[rna_fields(k,{'select_control_point','select_left_handle','select_right_handle'}) for k in f.keyframe_points],
            'modifiers':modifiers}


def clean_owned_copy(rig):
    groups=defaultdict(list)
    for f in rig.animation_data.drivers:
        groups[(f.data_path,f.array_index)].append(f)
    remove=[]
    report={'missing_outputs':[], 'identical_duplicates':[], 'conflicts':[]}
    for (path,index), curves in groups.items():
        try:
            rig.path_resolve(path)
        except ValueError:
            report['missing_outputs'].append({'path':path,'index':index,'count':len(curves)})
            remove.extend(curves)
            continue
        if len(curves)>1:
            if all(signature(c)==signature(curves[0]) for c in curves[1:]):
                remove.extend(curves[1:])
                report['identical_duplicates'].append({'path':path,'index':index,'removed':len(curves)-1})
            else:
                report['conflicts'].append({'path':path,'index':index})
    if report['conflicts']:
        raise ValueError('Conflicting drivers require explicit review: '+str(report['conflicts']))
    report['before']=len(rig.animation_data.drivers)
    for f in remove:
        rig.animation_data.drivers.remove(f)
    report['after']=len(rig.animation_data.drivers)
    return report
