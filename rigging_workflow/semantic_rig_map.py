"""Contract C4 bone roles for exchange and native control roles for authoring."""

import json
import re
from pathlib import Path

SIDES = ("L", "R")
FINGERS = ("thumb", "index", "middle", "ring", "pinky")
LIMBS = ("shoulder", "upper_arm", "forearm", "hand", "thigh", "shin", "foot", "toe")
IK = ("ik_hand", "ik_foot", "ik_pole_arm", "ik_pole_leg")
BODY = ("root", "hips", "spine", "chest", "neck", "head")
ROLES = set(BODY) | {f"{role}.{side}" for side in SIDES for role in (*LIMBS, *IK)}
ROLES.update(f"{digit}.{segment:02}.{side}" for side in SIDES for digit in FINGERS for segment in range(1, 4))


def _one_name(bones, *names):
    return next((name for name in names if name in bones), None)


def _existing(bones, mapping):
    return {role: name for role, name in mapping.items() if name in bones}


def _human(bones):
    logical = dict(hips="spine", spine="spine.001", chest="spine.003",
                   neck="spine.004", head="spine.006", root="root")
    controls = dict(root="root", hips="hips", spine="torso", chest="chest", neck="neck", head="head")
    for side in SIDES:
        for part in LIMBS:
            logical[f"{part}.{side}"] = f"{part}.{side}"
            controls[f"{part}.{side}"] = f"{part}_fk.{side}" if part != "shoulder" else f"shoulder.{side}"
        for role, name in zip(IK, ("hand_ik", "foot_ik", "upper_arm_ik_target", "thigh_ik_target")):
            controls[f"{role}.{side}"] = f"{name}.{side}"
        for finger in FINGERS:
            stem = finger if finger == "thumb" else f"f_{finger}"
            for segment in range(1, 4):
                role = f"{finger}.{segment:02}.{side}"
                name = f"{stem}.{segment:02}.{side}"
                logical[role] = controls[role] = name
    return ({key: value for key, name in logical.items()
             if (value := _one_name(bones, f"DEF-{name}", f"ORG-{name}", name)) is not None},
            _existing(bones, controls))


def _c94(rig):
    from . import pose_switching, pose_switching_leg, retarget_c94

    record = pose_switching._metadata(rig)
    bones = rig.pose.bones
    controls = dict(root="根骨骼", hips="盆骨控制器", spine="腰椎控制器",
                    chest="胸腔控制器", neck="颈椎控制器", head="头颅控制器")
    logical = {}
    for role, _, name, _ in retarget_c94.rows(
            {key: key for key in (*BODY,
                                  *(f"{part}.{side}" for part in (*LIMBS, "toes") for side in SIDES),
                                  *(f"{digit}{index}.{side}" for digit in FINGERS for index in range(1, 5)
                                    for side in SIDES))}):
        if role.startswith("knee."):
            continue
        role = from_profile_role(role)
        controls[role] = name
    for side in SIDES:
        leg = pose_switching_leg._spec(side)
        controls.update({f"shoulder.{side}": f"肩膀.{side}",
                         f"ik_hand.{side}": f"手掌IK-目标.{side}", f"ik_pole_arm.{side}": f"大臂IK-极点.{side}",
                         f"ik_foot.{side}": leg["main"], f"ik_pole_leg.{side}": leg["pole"]})
        for role, stem in zip(("upper_arm", "forearm", "hand"), ("机制-大臂", "机制-小臂", "手掌")):
            mixed = f"{stem}.{side}"
            name = next((deform for deform, mechanism in record["mechanism_mapping"].items()
                         if mechanism == mixed), mixed)
            logical[f"{role}.{side}"] = name
    return _existing(bones, {**controls, **logical}), _existing(bones, controls)


def from_profile_role(role):
    """Translate legacy profile spellings without changing its saved schema."""
    segment = re.fullmatch(r"(thumb|index|middle|ring|pinky)([1-3])\.([LR])", role)
    if segment:
        digit, number, side = segment.groups()
        return f"{digit}.{int(number):02}.{side}"
    return role.replace("toes.", "toe.", 1) if role.startswith("toes.") else role


def to_profile_map(document):
    mapping = {}
    for role, bone in document["bones"].items():
        if role == "root" or role.startswith("ik_"):
            continue
        if role.startswith("toe."):
            role = role.replace("toe.", "toes.", 1)
        elif role.count(".") == 2:
            digit, segment, side = role.split(".")
            role = f"{digit}{int(segment)}.{side}"
        mapping[role] = bone
    return mapping


def validate(rig, document):
    if (not isinstance(document, dict) or document.get("schema_version") != "1.0"
            or document.get("rig") != rig.name or document.get("kind") not in {"HUMAN", "C94", "GENERIC_FK"}
            or set(document) != {"schema_version", "rig", "kind", "bones", "controls"}):
        raise ValueError("Invalid semantic rig map or mismatched rig name")
    for group in ("bones", "controls"):
        mapping = document[group]
        if (not isinstance(mapping, dict) or set(mapping) - ROLES
                or any(not isinstance(value, str) or value not in rig.pose.bones for value in mapping.values())):
            raise ValueError(f"Invalid or missing {group} in the semantic rig map")
    if not document["bones"]:
        raise ValueError("The semantic rig map needs at least one bone")
    return document


def describe(rig, explicit=None):
    if explicit is not None:
        return validate(rig, explicit)
    if rig.type != "ARMATURE":
        raise ValueError("A semantic map requires an armature")
    if "rigging_modular_v1" in rig:
        from .modular_rig import read_generated

        if read_generated(rig)["kind"] != "HUMAN":
            raise ValueError("Contract C4 semantic map requires a biped")
        kind = "HUMAN"
        bones, controls = _human(rig.pose.bones)
    elif "c94_generated_rig_v1" in rig:
        kind = "C94"
        bones, controls = _c94(rig)
    else:
        from .retarget_source import propose_fbx_mapping

        kind = "GENERIC_FK"
        proposed = propose_fbx_mapping(rig)
        bones = {from_profile_role(role): name for role, name in proposed["bones"].items()
                 if from_profile_role(role) in ROLES}
        if proposed["root"] and proposed["root"] not in bones.values():
            bones["root"] = proposed["root"]
        controls = dict(bones)
    from . import control_layer

    if control_layer.CONTROL_KEY in rig:
        by_source = {item["source"]: item["bone"] for item in control_layer.read(rig)["controls"].values()}
        controls = {role: by_source.get(name, name) for role, name in controls.items()}
    return validate(rig, {"schema_version": "1.0", "rig": rig.name, "kind": kind,
                          "bones": {**bones, **{key: controls[key] for key in controls if key.startswith("ik_")}},
                          "controls": controls})


def write(rig, path, explicit=None):
    document = describe(rig, explicit)
    with Path(path).open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(document, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")
    return document
