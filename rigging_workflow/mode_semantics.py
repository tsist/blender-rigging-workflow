# SPDX-License-Identifier: GPL-3.0-or-later
"""Native C94 input meanings; no mode writes, space matching or reset policy."""
import math


def catalog():
    rows = []
    def add(bone, key, group, label, help_text):
        rows.append({'bone': bone, 'property': key, 'group': group,
                     'label': label, 'help': help_text})
    for side, word in (('L', '左'), ('R', '右')):
        for limb in ('臂', '腿'):
            add('骨架设置', word + limb + 'FK-IK', word + limb, word + limb + 'FK-IK',
                '0为FK，1为IK，中间值混合两套输入。拖动属性不会匹配姿态；保姿态按钮需从完整0或1端点使用。切换不会自动插帧。')
            add('骨架设置', word + limb + '跟随', word + limb, word + limb + '跟随',
                ('0隔离躯干旋转，1跟随躯干，2再跟随肩膀；0–1和1–2分别混合。位置和缩放仍跟随，0不代表世界锁定。'
                 if limb == '臂' else '0隔离躯干旋转，1完全跟随，中间值混合。位置和缩放仍跟随，0不代表世界锁定。'))
        add('骨架设置', word + '脚脚趾连接', word + '腿', word + '脚脚趾连接',
            '控制脚趾根与跖骨的位置、旋转连接权重，独立于腿FK/IK。抬脚时按造型需要减小；保姿态切换会保留当前值。')
        for finger in ('大拇指', '食指', '中指', '无名指', '小拇指'):
            add(finger + 'IK.' + side, '手指IK', word + '手', finger + 'IK',
                '有效IK权重为本指权重乘同侧手臂FK-IK。手臂为FK时，本指设为1也不会启用IK；没有独立手指保姿态匹配。')
        add('眼部.' + side, word + '眼睑跟随', '面部', word + '眼睑跟随',
            '控制随视线的眼睑响应，不是眨眼开关。形态键路径控制视线Key，原版骨控路径控制眼睑跟随约束。')
    add('骨架设置', '肩膀IK跟随', '躯干', '肩膀IK跟随',
        '原版使用约0.99附近的阈值门，本机0.99已触发，并非连续强度。请用0或1明确关闭、开启，避免在临界值附近调节。')
    add('骨架设置', '臀部跟随', '躯干', '臀部跟随',
        '混合大腿带动臀部的多组矫正，分支强度不同；不是胸臀弹性模拟的总开关。')
    add('骨架设置', '躯干拉伸', '躯干', '躯干拉伸',
        '控制多段躯干拉伸约束的混合权重，0关闭拉伸贡献，1完全启用；不会恢复控件位置。')
    add('骨架设置', '颈椎头部跟随', '头部', '颈椎头部跟随',
        '混合头、颈对上身旋转的跟随。0隔离旋转、1完全跟随；位置和缩放仍跟随，中间值按原生层级求值。')
    add('下巴控制器', '嘴部张合', '面部', '嘴部张合',
        '原版1使上下颚控制趋向闭合锁定，0保留各自控制，中间值混合；名称不是张嘴幅度。')
    add('眼部控制器', '眼部父级切换', '面部', '眼部父级切换',
        '0不选两骨目标，1跟根骨骼，2跟头部面部。所有状态仍随骨架对象移动；直接改值不自动保持视线位置。')
    add('骨架设置', '脸部控制形态键-骨骼', '面部', '脸部控制形态键-骨骼',
        '0为原版形态键路径，1为原版骨控路径。它只门控视线、眼睑和睫毛部分路径，闭眼等Key仍可叠加。请在中立、无冲突输入时调整；不代表新角色完整面部已制作完成。')
    add('骨架设置', '头发FK-IK', '头发', '头发FK-IK',
        '控制发链约束强度，不同分支有倍率和钳制，不等于四肢的FK/IK模式。独立于前、后发Cloth开关。')
    for region in ('前发', '后发'):
        add('骨架设置', region + '物理', '头发', region + '物理',
            '0关闭、1开启原版对应Cloth修改器求值。不是烘焙、重置或缓存有效性证明；本角色须已有相应模拟模块。')
    return rows


_CATALOG = {(r['bone'], r['property']): r for r in catalog()}


def describe(bone, key):
    return _CATALOG.get((bone, key))


def available(rig):
    has_hair = any('头发' in c.name and len(c.bones) for c in rig.data.collections_all)
    result = []
    for row in _CATALOG.values():
        if row['group'] == '头发' and not has_hair:
            continue
        pb = rig.pose.bones.get(row['bone'])
        value = pb.get(row['property']) if pb else None
        if type(value) in (int, float, bool) and math.isfinite(float(value)):
            result.append(dict(row))
    return result


def info_lines(rig, bone, key):
    row = describe(bone, key)
    if row is None:
        raise ValueError('该属性没有原生控制说明')
    pb = rig.pose.bones.get(bone)
    if pb is None or key not in pb:
        raise ValueError('角色已不包含该控制属性')
    lines = ['当前值：' + str(pb[key]), row['help']]
    if key == '手指IK':
        word = '左' if bone.endswith('.L') else '右'
        settings = rig.pose.bones.get('骨架设置')
        if settings is not None and word + '臂FK-IK' in settings:
            effective = float(pb[key]) * float(settings[word + '臂FK-IK'])
            lines.insert(1, '当前有效IK权重：' + format(effective, '.4g') + '（本指 × ' + word + '臂）')
    return lines
