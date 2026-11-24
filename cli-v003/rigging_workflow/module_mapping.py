"""Native C94 parent/readout evidence for disabled-module weight ownership.

No geometric nearest-bone query or name-based anatomical guessing is used.
Some deformation bones read a sibling control chain rather than parenting to
another deformation bone; their exact COPY_TRANSFORMS relation is the bridge.
"""

PARENT = 'nearest_retained_deform_ancestor'
BRIDGE = 'retained_control_deform_bridge_v1'
_CONSTRAINT_FIELDS = {'owner', 'name', 'type', 'target', 'subtarget', 'owner_space',
                      'target_space', 'mix_mode', 'mute', 'influence', 'head_tail', 'remove_target_shear'}


def _readout(source, name):
    pose = source.pose.bones[name]
    active = [c for c in pose.constraints if not c.mute and abs(c.influence) > 1e-9]
    if len(active) != 1:
        return None
    c = active[0]
    if (c.type != 'COPY_TRANSFORMS' or c.target != source or not c.subtarget
            or c.owner_space != 'WORLD' or c.target_space != 'WORLD'
            or c.mix_mode != 'REPLACE' or c.influence != 1.0 or c.head_tail != 0.0):
        return None
    prefix = c.path_from_id()
    # A current influence of one alone is not evidence of an invariant readout.
    for owner in (source, source.data):
        ad = owner.animation_data
        if ad and any(fc.data_path == prefix or fc.data_path.startswith(prefix + '.') for fc in ad.drivers):
            return None
    return {'owner': name, 'name': c.name, 'type': c.type, 'target': 'SELF',
            'subtarget': c.subtarget, 'owner_space': c.owner_space, 'target_space': c.target_space,
            'mix_mode': c.mix_mode, 'mute': bool(c.mute), 'influence': float(c.influence),
            'head_tail': float(c.head_tail), 'remove_target_shear': bool(c.remove_target_shear)}


def build(source, keep, removed):
    """Return source-deform -> retained-deform and independently replayable proof."""
    keep, removed = set(keep), set(removed)
    readouts = {}
    for name in sorted(keep):
        if source.data.bones[name].use_deform:
            proof = _readout(source, name)
            if proof:
                readouts.setdefault(proof['subtarget'], []).append(proof)
    mapping, evidence = {}, {}
    for name in sorted(removed):
        bone = source.data.bones[name]
        if not bone.use_deform:
            continue
        path = [{'bone': name, 'retained': False, 'use_deform': True}]
        parent = bone.parent
        direct = None
        while parent:
            path.append({'bone': parent.name, 'retained': parent.name in keep, 'use_deform': bool(parent.use_deform)})
            if parent.name in keep and parent.use_deform:
                direct = parent.name
                break
            parent = parent.parent
        if direct is not None:
            mapping[name] = direct
            evidence[name] = {'method': PARENT, 'path': path}
            continue
        bridge = None
        for index, node in enumerate(path[1:], 1):
            candidates = readouts.get(node['bone'], []) if node['retained'] else []
            if len(candidates) > 1:
                raise ValueError('关闭模块的控制祖先有多个保留形变读出，不能猜映射：' + name)
            if len(candidates) == 1:
                bridge = candidates[0]
                path = path[:index + 1]
                break
        if bridge is None:
            raise ValueError('关闭形变骨没有可靠的保留形变祖先或原生控制读出：' + name)
        mapping[name] = bridge['owner']
        evidence[name] = {'method': BRIDGE, 'path': path, 'bridge': bridge}
    return mapping, evidence


def validate_evidence(source, destination, row, keep, removed):
    """Validate serialized proof. Native replay against base bytes is separate."""
    if not isinstance(row, dict) or row.get('method') not in {PARENT, BRIDGE}:
        raise ValueError('模块权重映射缺少已支持的原生依据：' + source)
    method = row['method']
    if set(row) != ({'method', 'path'} if method == PARENT else {'method', 'path', 'bridge'}):
        raise ValueError('模块权重映射依据结构无效：' + source)
    path = row['path']
    if not isinstance(path, list) or len(path) < 2:
        raise ValueError('模块权重映射缺少完整父链：' + source)
    for item in path:
        if (not isinstance(item, dict) or set(item) != {'bone', 'retained', 'use_deform'}
                or not isinstance(item['bone'], str) or item['bone'] not in keep | removed
                or type(item['retained']) is not bool or type(item['use_deform']) is not bool
                or item['retained'] != (item['bone'] in keep)):
            raise ValueError('模块权重映射父链损坏：' + source)
    if (path[0]['bone'] != source or not path[0]['use_deform']
            or len({item['bone'] for item in path}) != len(path)
            or any(item['retained'] and item['use_deform'] for item in path[1:-1])):
        raise ValueError('模块权重映射父链无效：' + source)
    if method == PARENT:
        if path[-1]['bone'] != destination or not path[-1]['use_deform']:
            raise ValueError('模块权重映射不是保留形变祖先：' + source)
        return
    bridge = row['bridge']
    if (not isinstance(bridge, dict) or set(bridge) != _CONSTRAINT_FIELDS
            or bridge['owner'] != destination or not isinstance(bridge['name'], str) or not bridge['name']
            or bridge['type'] != 'COPY_TRANSFORMS' or bridge['target'] != 'SELF'
            or bridge['subtarget'] != path[-1]['bone'] or not path[-1]['retained'] or path[-1]['use_deform']
            or bridge['owner_space'] != 'WORLD' or bridge['target_space'] != 'WORLD'
            or bridge['mix_mode'] != 'REPLACE' or bridge['mute'] is not False
            or type(bridge['influence']) not in (int, float) or bridge['influence'] != 1
            or type(bridge['head_tail']) not in (int, float) or bridge['head_tail'] != 0
            or type(bridge['remove_target_shear']) is not bool):
        raise ValueError('模块权重的保留控制/形变读出依据损坏：' + source)
