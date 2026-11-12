"""SHA-bound native C94 module libraries shared by manual and batch entries.

The library stores only its definition; its own byte hash belongs in the scene
record, avoiding a self-referential file identity. No source rig is edited.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import uuid

import bpy

KEY = 'c94_module_configuration_v1'
DEFINITION_KEY = 'c94_module_definition_v1'
SCHEMA = 'c94.module-configuration.v1'
DEFINITION_SCHEMA = 'c94.module-definition.v1'
_OPTIONAL = ('face', 'hair', 'breast', 'glute', 'toes')
_MODULES = {'core', *_OPTIONAL, 'skirt', 'generic_accessories'}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _descriptor(path):
    return {'file': str(path), 'expected_sha256': _hash(path)}


def _sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _file_record(value):
    if (not isinstance(value, dict) or set(value) != {'file', 'expected_sha256'}
            or not isinstance(value['file'], str) or not _sha(value['expected_sha256'])):
        raise ValueError('模块配置的文件身份无效')
    path = Path(value['file'])
    if not path.is_absolute() or path.suffix.lower() != '.blend':
        raise ValueError('模块配置需要明确的.blend绝对路径')


def _enabled(value):
    if (not isinstance(value, dict) or set(value) - _MODULES
            or not set(_OPTIONAL) <= set(value) or any(type(v) is not bool for v in value.values())):
        raise ValueError('模块选择须明确填写所有可选项的bool值，不接受未知字段')
    result = {k: value.get(k, k == 'core') for k in sorted(_MODULES)}
    if not result['core']:
        raise ValueError('身体、四肢及手部核心模块必须保留')
    if result['hair'] or result['skirt'] or result['generic_accessories']:
        raise ValueError('当前配置库尚不支持新头发、裙摆或通用附属模块')
    return result


def _definition(value):
    fields = {'schema', 'base_template', 'reference', 'enabled_modules', 'retained_bones',
              'removed_bones', 'merge_mapping', 'mapping_evidence'}
    if not isinstance(value, dict) or set(value) != fields or value['schema'] != DEFINITION_SCHEMA:
        raise ValueError('模块定义结构或版本无效')
    _file_record(value['base_template'])
    ref = value['reference']
    if (not isinstance(ref, dict) or set(ref) != {'object', 'signature'}
            or not isinstance(ref['object'], str) or not ref['object'] or not _sha(ref['signature'])):
        raise ValueError('模块配置参考骨身份无效')
    if _enabled(value['enabled_modules']) != value['enabled_modules']:
        raise ValueError('模块定义缺少规范化的明确模块选择')
    for field in ('retained_bones', 'removed_bones'):
        names = value[field]
        if (not isinstance(names, list) or any(not isinstance(n, str) or not n for n in names)
                or names != sorted(set(names))):
            raise ValueError('模块骨列表无效：' + field)
    keep, removed = set(value['retained_bones']), set(value['removed_bones'])
    if not keep or keep & removed:
        raise ValueError('模块保留/关闭骨集合无效')
    mapping, evidence = value['merge_mapping'], value['mapping_evidence']
    if (not isinstance(mapping, dict) or not isinstance(evidence, dict) or set(mapping) != set(evidence)
            or not set(mapping) <= removed or any(v not in keep for v in mapping.values())):
        raise ValueError('模块权重归并映射无效')
    for source, destination in mapping.items():
        from . import module_mapping
        module_mapping.validate_evidence(source, destination, evidence[source], keep, removed)
    return value


def _record(value):
    if (not isinstance(value, dict) or set(value) != {'schema', 'id', 'definition', 'configured'}
            or value['schema'] != SCHEMA or not _sha(value['id'])):
        raise ValueError('模块配置记录结构或版本无效')
    _definition(value['definition'])
    expected = hashlib.sha256(_canonical(value['definition']).encode('utf-8')).hexdigest()
    if value['id'] != expected:
        raise ValueError('模块配置定义与id不一致')
    _file_record(value['configured'])
    if Path(value['configured']['file']) == Path(value['definition']['base_template']['file']):
        raise ValueError('配置库必须独立于原模板库')
    return copy.deepcopy(value)


def read(scene):
    """Read a persisted JSON record without importing a library or changing IDs."""
    raw = scene.get(KEY)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError('场景模块配置不是JSON记录')
    try:
        return _record(json.loads(raw))
    except (TypeError, KeyError, json.JSONDecodeError) as error:
        raise ValueError('场景模块配置损坏') from error


def _context(scene, reference):
    if scene != bpy.context.scene or bpy.context.mode != 'OBJECT':
        raise ValueError('模块配置需要当前Scene的对象模式')
    if (not isinstance(reference, bpy.types.Object) or reference.type != 'ARMATURE'
            or scene.objects.get(reference.name) != reference):
        raise ValueError('模块配置需要当前Scene的明确完整参考骨')
    bpy.context.view_layer.update()


def validate(scene, reference, template_file, configuration):
    """Validate record, source bytes and current reference before native import.

    The generator also checks the actual imported definition and bone set.
    """
    _context(scene, reference)
    record = _record(configuration)
    definition = record['definition']
    path = Path(template_file).resolve(strict=True)
    if path != Path(definition['base_template']['file']).resolve(strict=True):
        raise ValueError('请传入配置绑定的原模板库路径')
    for descriptor in (definition['base_template'], record['configured']):
        if _hash(descriptor['file']) != descriptor['expected_sha256']:
            raise ValueError('模块配置的模板库已改变，请重新准备配置：' + descriptor['file'])
    from . import positioning
    if (reference.name != definition['reference']['object']
            or positioning.reference_signature(reference) != definition['reference']['signature']):
        raise ValueError('模块配置绑定的参考骨已改变，请重新准备配置')
    _verify_native_definition(scene, definition)
    return record


def validate_collection(collection, rig, configuration):
    """Verify the actual native library against its already validated record."""
    raw = collection.get(DEFINITION_KEY)
    if not isinstance(raw, str):
        raise ValueError('配置模板库缺少明确模块定义')
    try:
        definition = _definition(json.loads(raw))
    except (TypeError, KeyError, json.JSONDecodeError) as error:
        raise ValueError('配置模板库的模块定义损坏') from error
    if (_canonical(definition) != _canonical(configuration['definition'])
            or set(rig.data.bones.keys()) != set(definition['retained_bones'])):
        raise ValueError('配置模板库的定义或实际骨集合与配置记录不一致')


def _ids():
    """Snapshot all actual ID collections, including indirect library imports."""
    result = set()
    for prop in bpy.data.bl_rna.properties:
        if prop.type == 'COLLECTION':
            result.update(item for item in getattr(bpy.data, prop.identifier) if isinstance(item, bpy.types.ID))
    return result


def _own(owned_ids, *blocks):
    """Record only IDs returned by this operation's new/copy calls."""
    if owned_ids is not None:
        if not isinstance(owned_ids, list):
            raise ValueError('owned_ids须为调用者的可写list或None')
        for block in blocks:
            owned_ids.append(block)
            if isinstance(block, bpy.types.Mesh) and block.shape_keys:
                owned_ids.append(block.shape_keys)
    return blocks[0] if len(blocks) == 1 else blocks


def _cleanup(before, active, selected, *, owned_ids=None):
    live = _ids()
    if owned_ids is None:
        added = live - before
    else:
        added = set()
        for item in owned_ids:
            try:
                if item in live and item not in before:
                    added.add(item)
            except ReferenceError:
                pass
    if bpy.context.object in added and bpy.context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    if added:
        bpy.data.batch_remove(ids=tuple(added))
    for obj in list(bpy.context.selected_objects):
        obj.select_set(False)
    for obj in selected:
        if bpy.context.view_layer.objects.get(obj.name) == obj:
            obj.select_set(True)
    bpy.context.view_layer.objects.active = active
    bpy.context.view_layer.update()


def _load(scene, path, *, owned_ids=None):
    from . import generation
    if owned_ids is not None and not isinstance(owned_ids, list):
        raise ValueError('owned_ids须为调用者的可写list或None')
    before = _ids()
    target = None
    try:
        with bpy.data.libraries.load(str(path), link=False) as (source, target):
            names = [n for n in source.collections if n.endswith('__模板')]
            if len(names) != 1:
                raise ValueError('模板库须包含一个明确的C94模板集合')
            target.collections = names
    finally:
        if target is not None and owned_ids is not None:
            owned_ids.extend(generation._returned_import_ids(
                getattr(target, 'collections', ()), before, Path(path).resolve()))
    collection = target.collections[0]
    if not collection or not collection.get('c94_private_template'):
        raise ValueError('输入不是C94私有模板库')
    scene.collection.children.link(collection)
    rigs = [o for o in collection.all_objects if o.type == 'ARMATURE']
    if len(rigs) != 1:
        raise ValueError('模板集合须恰有一个C94骨架')
    return collection, rigs[0]


def _verify_native_definition(scene, definition):
    """Replay closure and readout proof against the SHA-checked base library."""
    from . import inventory, modules, module_mapping, template
    before = _ids()
    owned = []
    active, selected = bpy.context.view_layer.objects.active, list(bpy.context.selected_objects)
    try:
        collection, source = _load(scene, definition['base_template']['file'], owned_ids=owned)
        if DEFINITION_KEY in collection:
            raise ValueError('模块定义的base不是原始私有模板')
        plan = modules.build_module_plan(inventory.inspect_source(source))
        test = _own(owned, bpy.data.collections.new('C94_配置核验_' + uuid.uuid4().hex[:12]))
        scene.collection.children.link(test)
        _, keep, removed, _ = template._preflight(source, plan, definition['enabled_modules'], test, test.name)
        mapping, evidence = module_mapping.build(source, keep, removed)
        if (sorted(keep) != definition['retained_bones'] or sorted(removed) != definition['removed_bones']
                or mapping != definition['merge_mapping'] or _canonical(evidence) != _canonical(definition['mapping_evidence'])):
            raise ValueError('模块配置闭包或原生父链/控制读出与base库不一致')
    finally:
        _cleanup(before, active, selected, owned_ids=owned)


def prepare(scene, reference, template_file, enabled_modules, output_file):
    """Extract and atomically publish a new private library, then commit JSON.

    Imported/copied IDs are discarded before committing. Failure retains the
    previous scene record and never overwrites an existing destination.
    """
    _context(scene, reference)
    enabled = _enabled(enabled_modules)
    base = Path(template_file).resolve(strict=True)
    output = Path(output_file)
    if base.suffix.lower() != '.blend' or not output.is_absolute() or output.suffix.lower() != '.blend':
        raise ValueError('原模板和新配置库须为明确.blend路径，输出必须为绝对路径')
    output = output.parent.resolve(strict=True) / output.name
    if output.exists():
        raise ValueError('新配置库目标已存在；请选择新路径，不覆盖已有结果')
    from . import generation, inventory, modules, module_mapping, positioning, template, template_bundle
    signature = generation.validate_input(scene, reference)
    base_descriptor = _descriptor(base)
    before = _ids()
    owned = []
    active, selected = bpy.context.view_layer.objects.active, list(bpy.context.selected_objects)
    temp = output.with_name('.' + output.stem + '-' + uuid.uuid4().hex + '.blend')
    published = False
    record = None
    old_present, old_record = KEY in scene, scene.get(KEY)
    try:
        try:
            base_collection, source = _load(scene, base, owned_ids=owned)
            if DEFINITION_KEY in base_collection:
                raise ValueError('prepare需要原始私有模板库，不能把已有配置库冒充base模板')
            plan = modules.build_module_plan(inventory.inspect_source(source))
            collection = _own(owned, bpy.data.collections.new('C94_配置_' + uuid.uuid4().hex[:12] + '__模板'))
            scene.collection.children.link(collection)
            _, keep, removed, _ = template._preflight(source, plan, enabled, collection, collection.name[:-4])
            if not set(source.data.bones.keys()) <= set(reference.data.bones.keys()):
                raise ValueError('完整参考骨缺少原模板骨，不能准备模块配置')
            mapping, evidence = module_mapping.build(source, keep, removed)
            definition = {'schema': DEFINITION_SCHEMA, 'base_template': base_descriptor,
                          'reference': {'object': reference.name, 'signature': signature},
                          'enabled_modules': enabled, 'retained_bones': sorted(keep), 'removed_bones': sorted(removed),
                          'merge_mapping': mapping, 'mapping_evidence': evidence}
            _definition(definition)
            config_id = hashlib.sha256(_canonical(definition).encode('utf-8')).hexdigest()
            extracted = template.extract_template(source, plan, enabled, collection, collection.name[:-4], owned_ids=owned)
            collection[DEFINITION_KEY] = _canonical(definition)
            template_bundle.write_private_bundle(extracted['object'], source, collection, str(temp), owned_ids=owned)
            configured = {'file': str(output), 'expected_sha256': _hash(temp)}
            record = {'schema': SCHEMA, 'id': config_id, 'definition': definition, 'configured': configured}
            _record(record)
            loaded_collection, loaded_rig = _load(scene, temp, owned_ids=owned)
            validate_collection(loaded_collection, loaded_rig, record)
        finally:
            _cleanup(before, active, selected, owned_ids=owned)
        if (_hash(base) != base_descriptor['expected_sha256']
                or positioning.reference_signature(reference) != signature):
            raise ValueError('配置准备期间原模板或参考骨改变，未提交配置')
        # A hard link atomically creates a new name and cannot replace a racing
        # writer's existing destination. Both names live in the same directory.
        os.link(str(temp), str(output))
        published = True
        temp.unlink()
        scene[KEY] = _canonical(record)
        return copy.deepcopy(record)
    except Exception:
        if scene.get(KEY) != old_record or (KEY in scene) != old_present:
            if old_present:
                scene[KEY] = old_record
            elif KEY in scene:
                del scene[KEY]
        if published and output.exists() and record and _hash(output) == record['configured']['expected_sha256']:
            output.unlink()
        raise
    finally:
        if temp.exists():
            temp.unlink()
