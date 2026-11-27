# SPDX-License-Identifier: GPL-3.0-or-later
"""Known target policies for one Action/preview lifecycle."""


def implementation(record):
    kind = record.get('kind') if isinstance(record, dict) else None
    if kind == 'GENERIC_FK_V1':
        from . import retarget_generic_target
        return retarget_generic_target
    if kind == 'MODULAR_HUMAN_FK_V1':
        from . import retarget_modular_target
        return retarget_modular_target
    raise ValueError('未知的动作目标策略，请通过对应角色入口重新准备')
