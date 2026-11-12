"""Read native C94 structure without executing embedded text or changing poses.

All quantities describe the source; membership is not permission to remove bones.
"""
import re

import bpy


def id_ref(value):
    if value is None:
        return None
    return {"type": value.bl_rna.identifier, "name": value.name,
            "library": value.library.filepath if value.library else None}


def plain(value):
    if isinstance(value, bpy.types.ID):
        return {"id": id_ref(value)}
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    if hasattr(value, "to_dict"):
        return {str(k): plain(v) for k, v in value.to_dict().items()}
    if hasattr(value, "to_list"):
        return [plain(v) for v in value.to_list()]
    try:
        return [plain(v) for v in value]
    except TypeError:
        return str(value)


def properties(owner):
    return {k: plain(owner[k]) for k in owner.keys()}


def constraints(owner):
    rows = []
    for c in getattr(owner, "constraints", ()):
        values = {}
        for p in c.bl_rna.properties:
            k = p.identifier
            if k == "rna_type":
                continue
            v = getattr(c, k)
            if p.type == "POINTER":
                if isinstance(v, bpy.types.ID):
                    values[k] = {"id": id_ref(v)}
            elif p.type != "COLLECTION":
                values[k] = plain(v)
        if hasattr(c, "targets"):
            values["targets"] = [{"target": id_ref(t.target), "subtarget": t.subtarget,
                                  "weight": t.weight} for t in c.targets]
        rows.append(values)
    return rows


def constraint_references(rows):
    """Enumerate stored constraint ID targets without assuming optional pointers."""
    for c in rows:
        for field, subtarget, kind in (("target", "subtarget", "constraint"),
                                       ("pole_target", "pole_subtarget", "pole"),
                                       ("space_object", "space_subtarget", "constraint_space")):
            value = c.get(field) or {}
            ref = value.get("id")
            if ref:
                yield {"id": ref, "subtarget": c.get(subtarget),
                       "kind": kind, "constraint": c.get("name")}
        for target in c.get("targets") or ():
            if target.get("target"):
                yield {"id": target["target"], "subtarget": target.get("subtarget"),
                       "kind": "constraint_target", "constraint": c.get("name")}


def drivers(owner):
    ad = getattr(owner, "animation_data", None)
    if not ad:
        return []
    rows = []
    for f in ad.drivers:
        try:
            owner.path_resolve(f.data_path)
            output_resolves = True
        except (ValueError, KeyError, TypeError):
            output_resolves = False
        variables = []
        for v in f.driver.variables:
            targets = []
            for t in v.targets:
                valid_path = None
                if t.id and t.data_path:
                    try:
                        t.id.path_resolve(t.data_path)
                        valid_path = True
                    except (ValueError, KeyError, TypeError):
                        valid_path = False
                targets.append({"id": id_ref(t.id), "id_type": t.id_type,
                                "data_path": t.data_path, "bone_target": t.bone_target,
                                "transform_type": t.transform_type,
                                "transform_space": t.transform_space,
                                "rotation_mode": t.rotation_mode,
                                "path_resolves": valid_path})
            variables.append({"name": v.name, "type": v.type, "targets": targets})
        rows.append({"path": f.data_path, "index": f.array_index,
                     "type": f.driver.type, "expression": f.driver.expression,
                     "mute": f.mute, "is_valid": f.driver.is_valid,
                     "output_resolves": output_resolves, "variables": variables})
    return rows


def rig_record(rig):
    bones = []
    for b in rig.data.bones:
        p = rig.pose.bones[b.name]
        bones.append({
            "name": b.name, "parent": b.parent.name if b.parent else None,
            "head": list(b.head_local), "tail": list(b.tail_local),
            "matrix_local": [list(r) for r in b.matrix_local],
            "deform": b.use_deform, "connected": b.use_connect,
            "inherit_scale": b.inherit_scale, "inherit_rotation": b.use_inherit_rotation,
            "collections": [c.name for c in b.collections],
            "custom_shape": id_ref(p.custom_shape),
            "custom_shape_transform": p.custom_shape_transform.name if p.custom_shape_transform else None,
            "bbone_start": b.bbone_custom_handle_start.name if b.bbone_custom_handle_start else None,
            "bbone_end": b.bbone_custom_handle_end.name if b.bbone_custom_handle_end else None,
            "bbone_segments": b.bbone_segments,
            "rotation_mode": p.rotation_mode,
            "location": list(p.location), "rotation_euler": list(p.rotation_euler),
            "rotation_quaternion": list(p.rotation_quaternion), "scale": list(p.scale),
            "properties": properties(p), "constraints": constraints(p),
        })
    return {"object": rig.name, "data": rig.data.name,
            "object_id": id_ref(rig), "data_id": id_ref(rig.data),
            "matrix_world": [list(r) for r in rig.matrix_world],
            "parent": id_ref(rig.parent), "parent_bone": rig.parent_bone,
            "properties": properties(rig), "data_properties": properties(rig.data),
            "object_constraints": constraints(rig),
            "data_constraints": constraints(rig.data),
            "collections": [{"name": c.name, "parent": c.parent.name if c.parent else None,
                             "visible": c.is_visible, "bones": [b.name for b in c.bones]}
                            for c in rig.data.collections_all],
            "bones": bones, "drivers": drivers(rig), "data_drivers": drivers(rig.data),
            "action": id_ref(rig.animation_data.action) if rig.animation_data else None}


def inspect_source(rig):
    if not rig or rig.type != "ARMATURE":
        raise ValueError("请选择实际角色主骨架")
    record = rig_record(rig)
    consumers = []
    external_rigs = set()
    main_ids = {(ref["type"], ref["name"], ref["library"])
                for ref in (id_ref(rig), id_ref(rig.data))}

    def is_main(ref):
        return bool(ref) and (ref.get("type"), ref.get("name"), ref.get("library")) in main_ids

    def driver_refs(rows):
        return [d for d in rows if any(is_main(t.get("id"))
                for v in d["variables"] for t in v["targets"])]

    def scoped_constraints(obj):
        rows = [("object", constraints(obj)), ("data", constraints(obj.data))]
        if obj.type == "ARMATURE":
            rows.extend(("pose:" + p.name, constraints(p)) for p in obj.pose.bones)
        return [{"owner": scope, **ref} for scope, cs in rows
                for ref in constraint_references(cs)]

    for obj in bpy.data.objects:
        mods = [{"name": m.name, "object": m.object.name, "viewport": m.show_viewport,
                 "render": m.show_render} for m in obj.modifiers
                if m.type == "ARMATURE" and m.object == rig]
        own_drivers = drivers(obj)
        data_drivers = drivers(obj.data)
        key = obj.data.shape_keys if obj.type == "MESH" else None
        key_drivers = drivers(key) if key else []
        refs = driver_refs(own_drivers + data_drivers + key_drivers)
        cons = constraints(obj)
        constraint_refs = scoped_constraints(obj)
        depends = any(is_main(ref["id"]) for ref in constraint_refs)
        if mods or refs or obj.parent == rig or depends:
            consumers.append({"object": obj.name, "type": obj.type,
                              "armature_modifiers": mods, "parent": id_ref(obj.parent),
                              "parent_bone": obj.parent_bone, "driver_references": refs,
                              "constraint_references": constraint_refs,
                              "constraints": cons, "shape_keys": id_ref(key),
                              "vertex_groups": list(obj.vertex_groups.keys()) if obj.type == "MESH" else [],
                              "vertices": len(obj.data.vertices) if obj.type == "MESH" else None})
            if obj.type == "ARMATURE" and obj != rig:
                external_rigs.add(obj)
    # Include direct source dependencies as well as rigs consuming the source.
    source_refs = [r["id"] for r in scoped_constraints(rig)]
    source_refs.extend(t.get("id") for d in record["drivers"] + record["data_drivers"]
                       for v in d["variables"] for t in v["targets"])
    source_refs.append(record["parent"])
    for ref in source_refs:
        if not ref or is_main(ref):
            continue
        for other in bpy.data.objects:
            if other.type != "ARMATURE" or other == rig:
                continue
            candidates = (id_ref(other), id_ref(other.data))
            if ref in candidates:
                external_rigs.add(other)
    widgets = {p.custom_shape for p in rig.pose.bones if p.custom_shape}
    widget_rows = [{"object": w.name, "type": w.type, "data": id_ref(w.data),
                    "vertices": len(w.data.vertices) if w.type == "MESH" else None,
                    "parent": id_ref(w.parent), "constraints": constraints(w)}
                   for w in sorted(widgets, key=lambda x: x.name)]
    return {"schema": "c94.source-inventory.v1", "blender": bpy.app.version_string,
            "consumer_scan_version": 2,
            "source_file": bpy.data.filepath, "frame": bpy.context.scene.frame_current,
            "autoexec_enabled": bpy.context.preferences.filepaths.use_scripts_auto_execute,
            "main": record, "dependent_rigs": [rig_record(r) for r in sorted(external_rigs, key=lambda x:x.name)],
            "consumers": consumers, "widgets": widget_rows,
            "scope": "native stored structure; no source Text executed; not a template or motion acceptance"}
