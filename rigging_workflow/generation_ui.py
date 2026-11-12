"""Explicit local-template generation entry using the shared C94 generator."""
import json
import os
import traceback
from pathlib import Path

import bpy
from bpy.props import BoolProperty, PointerProperty, StringProperty

from . import positioning

OPTIONS = 'c94_generation_options_v1'
RESULT = 'c94_generation_ui_result_v1'
INPUT = 'c94_generation_ui_input_v1'
RIG_KEY = 'c94_generated_rig_v1'
_REGISTERED = []


def reference(context):
    state = positioning.read_state(context.scene)
    if not state:
        raise ValueError('先完成角色参考骨定位')
    rig = context.scene.objects.get(state.get('fitted_reference', ''))
    if not rig or rig.type != 'ARMATURE' or rig.get(positioning.OWNER_KEY) != state.get('owner'):
        raise ValueError('定位记录中的参考骨不存在或身份不匹配')
    options = getattr(context.scene, OPTIONS, None)
    name = options.reference_object.strip() if options else ''
    if name and name != rig.name:
        from . import accessory_ui
        return accessory_ui.reference(context.scene, rig, name)
    return rig


def _json(raw):
    try:
        result = json.loads(raw) if isinstance(raw, str) else None
        return result if isinstance(result, dict) else {}
    except ValueError:
        return {}


def existing(scene, ref, label, signature=None, configuration=None, template_file=None):
    configuration_id = configuration['id'] if configuration else None
    for obj in scene.objects:
        if obj.type != 'ARMATURE':
            continue
        generated = _json(obj.get(RIG_KEY))
        if not generated:
            continue
        if generated.get('configuration_id') != configuration_id:
            continue
        if generated.get('module_configuration') != configuration:
            continue
        actual_template = (generated.get('base_template_file') if configuration else
                           generated.get('template_file'))
        if template_file is not None and (not actual_template or
                os.path.normcase(os.path.abspath(actual_template)) !=
                os.path.normcase(os.path.abspath(str(template_file)))):
            continue
        inputs = _json(obj.get(INPUT))
        if inputs.get('reference_object') == ref.name and inputs.get('label') == label:
            if signature is None or inputs.get('reference_signature') == signature:
                return obj
        if (generated.get('reference_object', generated.get('reference')) == ref.name
                and generated.get('label') == label
                and (signature is None or generated.get('reference_signature') == signature)):
            return obj
    return None


def _activate(context, rig):
    if rig.name not in context.view_layer.objects or not rig.visible_get(view_layer=context.view_layer) or rig.hide_select:
        raise ValueError('生成角色在当前视图层不可见或不可选，请先显示它的集合')
    if context.object and context.object.mode != 'OBJECT':
        bpy.ops.object.mode_set(mode='OBJECT')
    for obj in context.view_layer.objects:
        obj.select_set(obj == rig, view_layer=context.view_layer)
    context.view_layer.objects.active = rig


class C94GEN_Options(bpy.types.PropertyGroup):
    template_file: StringProperty(name='私有模板库', subtype='FILE_PATH',
        description='选择有权使用的本机C94私有.blend模板库；代码包不附带角色资产')
    label: StringProperty(name='角色名称', default='Myona',
        description='用于新生成候选的名称；已有候选保留，不覆盖')
    use_configuration: BoolProperty(name='使用模块配置', default=False,
        description='先准备配置模板，再按同一配置生成或重建；旧模板生成入口保持可用')
    independent_toes: BoolProperty(name='独立脚趾', default=True,
        description='关闭时保留基本趾根与脚部机制，独立趾权重在重建副本归入同侧主骨')
    configuration_file: StringProperty(name='新配置模板文件', subtype='FILE_PATH',
        description='填写新建配置模板库的绝对.blend路径；已有文件不会覆盖')
    source_rig: StringProperty(name='原绑定骨架',
        description='选择本次保留的原C94骨架；只重建明确选中的网格副本')
    reference_object: StringProperty(name='本次完整参考',
        description='留空使用定位参考；附属链准备后指向独立完整参考副本，原参考保持')
    face: BoolProperty(name='面部模块', default=True)
    breast: BoolProperty(name='胸部附属模块', default=True)
    glute: BoolProperty(name='臀部附属模块', default=True)


def _module_options(options):
    return {'core':True,'face':options.face,'hair':False,'breast':options.breast,
            'glute':options.glute,'toes':options.independent_toes,'skirt':False,'generic_accessories':False}


def _configuration(context, ref, path):
    options = getattr(context.scene, OPTIONS)
    if not options.use_configuration:
        return None
    from . import module_configuration
    config = module_configuration.read(context.scene)
    if not config:
        raise ValueError('先准备本次模块配置模板')
    current = _module_options(options)
    if any(config['definition']['enabled_modules'].get(k) != v for k,v in current.items()):
        raise ValueError('模块选项已改变，请重新准备配置模板')
    from . import accessory_ui
    accessory_ui.reference_record(context.scene, ref, validate=True)
    return module_configuration.validate(context.scene, ref, str(path), config)


class C94GEN_OT_PrepareConfiguration(bpy.types.Operator):
    bl_idname = 'c94_workflow.prepare_module_configuration'
    bl_label = '准备模块配置模板'
    bl_description = '新建独立配置库并记录真实骨集合与归并映射；保留原库和完整参考骨'

    @classmethod
    def poll(cls, context):
        return bool(context.scene and context.mode == 'OBJECT')

    def execute(self, context):
        try:
            ref = reference(context)
            options = getattr(context.scene, OPTIONS)
            source = Path(bpy.path.abspath(options.template_file)).expanduser()
            output = Path(bpy.path.abspath(options.configuration_file)).expanduser()
            if (not options.template_file.strip() or not source.is_absolute()
                    or not options.configuration_file.strip() or not output.is_absolute()):
                raise ValueError('填写原模板和新配置模板的明确绝对路径')
            from . import module_configuration
            from . import accessory_ui
            accessory_ui.reference_record(context.scene, ref, validate=True)
            enabled = _module_options(options)
            config = module_configuration.prepare(context.scene, ref, str(source), enabled, str(output))
        except Exception as exc:
            self.report({'ERROR'}, '配置准备失败：' + str(exc))
            traceback.print_exc()
            return {'CANCELLED'}
        warnings = []
        try:
            options.use_configuration = True
        except Exception as exc:
            warnings.append(str(exc))
        definition = config['definition']
        self.report({'WARNING'} if warnings else {'INFO'}, '配置模板已保存：保留%d骨、关闭%d骨、%d项权重归并' % (
            len(definition['retained_bones']), len(definition['removed_bones']),
            len(definition['merge_mapping'])) + ('；入口提示：'+'；'.join(warnings) if warnings else ''))
        return {'FINISHED'}


class C94GEN_OT_Rebuild(bpy.types.Operator):
    bl_idname = 'c94_workflow.rebuild_bound_copy'
    bl_label = 'C94模块重建（保留兼容权重）'
    bl_description = '同参考C94的模块返工保留兼容权重；新附属方案或其它旧绑定请用角色新建与已有绑定重建入口'

    @classmethod
    def poll(cls, context):
        return bool(context.scene and context.mode == 'OBJECT')

    def execute(self, context):
        try:
            ref = reference(context)
            options = getattr(context.scene, OPTIONS)
            path = Path(bpy.path.abspath(options.template_file)).expanduser()
            config = _configuration(context, ref, path)
            if config is None:
                raise ValueError('此副本重建切片需要先准备明确模块配置')
            source = context.scene.objects.get(options.source_rig)
            meshes = [o for o in context.selected_objects if o.type == 'MESH']
            from . import rebuild
            output = rebuild.rebuild(context.scene, ref, str(path), config, source, meshes,
                                     label=options.label.strip())
        except Exception as exc:
            self.report({'ERROR'}, '副本重建失败：' + str(exc))
            traceback.print_exc()
            return {'CANCELLED'}
        warnings = []
        try:
            context.scene[RESULT] = json.dumps({'schema':'c94.rebuild-ui-result.v1',
                'rig_object':output['rig'].name, 'collection':output['collection'].name,
                'objects':[o.name for o in output['objects']], 'report':output['report']},
                ensure_ascii=False, allow_nan=False)
            _activate(context, output['rig'])
        except Exception as exc:
            warnings.append(str(exc))
        self.report({'WARNING'} if warnings else {'INFO'},
            '已保留原绑定并创建新骨架及%d件网格副本%s' % (len(output['objects']),
            '；结果/选择提示：'+'；'.join(warnings) if warnings else ''))
        return {'FINISHED'}


class C94GEN_OT_ChooseTemplate(bpy.types.Operator):
    bl_idname = 'c94_workflow.choose_generation_template'
    bl_label = '选择私有C94模板库'
    filepath: StringProperty(subtype='FILE_PATH')
    filter_glob: StringProperty(default='*.blend', options={'HIDDEN'})

    def invoke(self, context, event):
        self.filepath = getattr(context.scene, OPTIONS).template_file
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        getattr(context.scene, OPTIONS).template_file = bpy.path.abspath(self.filepath)
        return {'FINISHED'}


class C94GEN_OT_Generate(bpy.types.Operator):
    bl_idname = 'c94_workflow.generate_rig'
    bl_label = '生成完整C94骨架与基础面板'
    bl_description = '从当前参考骨和选定私有模板库生成新的角色候选；保留已有输出'

    @classmethod
    def poll(cls, context):
        if not context.scene or context.mode != 'OBJECT':
            return False
        try:
            reference(context)
            return True
        except ValueError:
            return False

    def execute(self, context):
        try:
            if context.mode != 'OBJECT':
                raise ValueError('请结束编辑或姿态模式，在对象模式执行生成')
            ref = reference(context)
            options = getattr(context.scene, OPTIONS)
            label = options.label.strip()
            if not label:
                raise ValueError('填写本次角色名称')
            path = Path(bpy.path.abspath(options.template_file)).expanduser()
            if not options.template_file.strip() or not path.is_absolute():
                raise ValueError('选择明确的本机私有模板库绝对路径')
            if not path.is_file() or path.suffix.lower() != '.blend':
                raise ValueError('私有模板库必须是存在的.blend文件：' + str(path))
            path = path.resolve(strict=True)
            signature = positioning.reference_signature(ref)
            config = _configuration(context, ref, path)
            previous = existing(context.scene, ref, label, signature,
                                configuration=config, template_file=path)
            if previous:
                raise ValueError('此参考骨已生成同名候选“' + previous.name + '”；可选择已有角色，或为新候选更换角色名称')
            # The UI never reimplements fitting, constraints or generation.
            from . import character_generation, accessory_ui
            output = character_generation.generate(context.scene, ref, str(path), label=label,
                configuration=config, chain_record=accessory_ui.reference_record(context.scene, ref, validate=True))
            if not isinstance(output, dict):
                raise RuntimeError('生成器没有返回有效结果；请检查本次生成记录')
            rig, collection, report = output.get('rig'), output.get('collection'), output.get('report')
            if not isinstance(rig, bpy.types.Object) or rig.type != 'ARMATURE' or rig.name not in context.scene.objects:
                raise RuntimeError('生成器没有交回场景内的角色骨架')
            if not isinstance(collection, bpy.types.Collection) or not _json(rig.get(RIG_KEY)):
                raise RuntimeError('生成器没有交回有效集合或角色生成记录')
        except Exception as exc:
            self.report({'ERROR'}, '生成失败：' + str(exc))
            traceback.print_exc()
            return {'CANCELLED'}
        # Generation has committed a valid, independently identifiable rig.
        # UI bookkeeping or selection must not turn that success into a
        # cancelled operator with a retained but apparently failed candidate.
        warnings = []
        try:
            rig[INPUT] = json.dumps({'schema': 'c94.generation-ui-input.v1',
                'reference_object': ref.name, 'reference_signature': signature,
                'template_file': str(path), 'label': label,
                'configuration_id':config['id'] if config else None}, ensure_ascii=False, allow_nan=False)
        except Exception as exc:
            warnings.append('入口记录未写入：' + str(exc))
            traceback.print_exc()
        try:
            record = {'schema': 'c94.generation-ui-result.v1', 'rig_object': rig.name,
                      'collection': collection.name, 'report': report}
            context.scene[RESULT] = json.dumps(record, ensure_ascii=False, allow_nan=False)
        except Exception as exc:
            warnings.append('结果摘要未写入：' + str(exc))
            traceback.print_exc()
        try:
            _activate(context, rig)
        except Exception as exc:
            warnings.append('未能自动激活：' + str(exc))
            traceback.print_exc()
        if warnings:
            self.report({'WARNING'}, '角色“' + rig.name + '”已生成并保留；' + '；'.join(warnings))
        else:
            self.report({'INFO'}, '已生成角色骨架与基础面板；下一阶段仍需蒙皮')
        return {'FINISHED'}


class C94GEN_OT_SelectExisting(bpy.types.Operator):
    bl_idname = 'c94_workflow.select_generated_rig'
    bl_label = '选择已有生成角色'

    def execute(self, context):
        try:
            ref = reference(context)
            options = getattr(context.scene, OPTIONS)
            config = _configuration(context, ref, Path(bpy.path.abspath(options.template_file)))
            rig = existing(context.scene, ref, options.label.strip(),
                           positioning.reference_signature(ref), configuration=config,
                           template_file=Path(bpy.path.abspath(options.template_file)))
            if not rig or not _json(rig.get(RIG_KEY)):
                raise ValueError('当前场景没有对应的已生成角色')
            _activate(context, rig)
            return {'FINISHED'}
        except (ValueError, RuntimeError) as exc:
            self.report({'ERROR'}, str(exc)); return {'CANCELLED'}


class C94GEN_PT_Generation(bpy.types.Panel):
    bl_label = 'C94角色生成'
    bl_idname = 'C94GEN_PT_generation'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = '角色绑定'
    bl_order = 1

    @classmethod
    def poll(cls, context):
        try:
            return bool(context.scene and positioning.read_state(context.scene))
        except ValueError:
            return False

    def draw(self, context):
        layout = self.layout
        workspace = getattr(context.scene, 'c94_workspace_options_v1', None)
        advanced = workspace is None or workspace.level != 'DAILY'
        try:
            ref = reference(context)
        except ValueError as exc:
            layout.label(text=str(exc), icon='INFO'); return
        options = getattr(context.scene, OPTIONS)
        layout.label(text='当前参考骨：' + ref.name, icon='ARMATURE_DATA')
        if advanced:
            layout.prop_search(options, 'reference_object', context.scene, 'objects')
        row = layout.row(align=True)
        row.prop(options, 'template_file', text='模板库')
        row.operator('c94_workflow.choose_generation_template', text='', icon='FILE_FOLDER')
        layout.prop(options, 'label')
        layout.label(text='模板资产由你在本机选择', icon='FILE_BLEND')
        box = layout.box()
        box.prop(options, 'use_configuration')
        box.prop(options, 'face')
        box.prop(options, 'breast')
        box.prop(options, 'glute')
        box.prop(options, 'independent_toes')
        box.prop(options, 'configuration_file')
        box.operator('c94_workflow.prepare_module_configuration', icon='FILE_NEW')
        box.label(text='关闭附属模块时保留身体、头部和四肢基本机制')
        if options.use_configuration:
            try:
                from . import module_configuration
                config = module_configuration.read(context.scene)
                if not config:
                    raise ValueError('先准备本次模块配置模板')
                definition = config['definition']
                box.label(text='模块配置已准备', icon='CHECKMARK')
                if advanced:
                    box.label(text='配置：保留%d骨 / 关闭%d骨' % (
                        len(definition['retained_bones']), len(definition['removed_bones'])))
                    box.label(text='权重归并：%d项' % len(definition['merge_mapping']))
                if any(definition['enabled_modules'].get(k)!=v for k,v in _module_options(options).items()):
                    box.label(text='选项已变化，需准备新配置', icon='ERROR')
                if advanced:
                    box.label(text='来源、参考和文件在提交时重新验证')
            except Exception as exc:
                box.label(text=str(exc), icon='ERROR')
        if context.mode != 'OBJECT':
            layout.label(text='请切到对象模式再生成', icon='INFO')
        if not advanced:
            layout.label(text='完成配置后，在下方新建角色并绑定网格', icon='INFO')
            return
        layout.operator('c94_workflow.generate_rig', icon='ARMATURE_DATA')
        candidate = None
        try:
            from . import module_configuration
            current = module_configuration.read(context.scene) if options.use_configuration else None
            if current or not options.use_configuration:
                candidate = existing(context.scene, ref, options.label.strip(), configuration=current,
                                     template_file=bpy.path.abspath(options.template_file))
        except (ValueError, TypeError, KeyError):
            pass  # The configuration box above displays malformed-record errors.
        if candidate:
            layout.label(text='已有候选：' + candidate.name, icon='CHECKMARK')
            layout.operator('c94_workflow.select_generated_rig', icon='RESTRICT_SELECT_OFF')
        layout.label(text='输出为完整C94骨架与基础面板', icon='INFO')
        layout.label(text='角色蒙皮仍需后续步骤')
        box = layout.box()
        box.label(text='已有C94模块返工：保留兼容权重')
        box.prop_search(options, 'source_rig', context.scene, 'objects')
        box.label(text='明确选中%d件网格；原绑定保留' % sum(o.type=='MESH' for o in context.selected_objects))
        box.operator('c94_workflow.rebuild_bound_copy', icon='DUPLICATE')
        box.label(text='当前支持：中立C94、无Key/非空旧动画的网格')
        box.label(text='新附属方案或其它旧绑定：使用下方角色新建入口')


_CLASSES = (C94GEN_Options, C94GEN_OT_ChooseTemplate, C94GEN_OT_PrepareConfiguration,
            C94GEN_OT_Rebuild, C94GEN_OT_Generate,
            C94GEN_OT_SelectExisting, C94GEN_PT_Generation)


def register():
    if _REGISTERED:
        return
    if hasattr(bpy.types.Scene, OPTIONS):
        raise RuntimeError('角色生成选项已由其它注册实例占用')
    try:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
            _REGISTERED.append(cls)
        setattr(bpy.types.Scene, OPTIONS, PointerProperty(type=C94GEN_Options))
    except Exception:
        unregister()
        raise


def unregister():
    if hasattr(bpy.types.Scene, OPTIONS):
        prop = bpy.types.Scene.bl_rna.properties.get(OPTIONS)
        if prop and prop.fixed_type.identifier == C94GEN_Options.bl_rna.identifier:
            delattr(bpy.types.Scene, OPTIONS)
    for cls in reversed(_REGISTERED):
        if getattr(cls, 'is_registered', False):
            bpy.utils.unregister_class(cls)
    _REGISTERED.clear()
