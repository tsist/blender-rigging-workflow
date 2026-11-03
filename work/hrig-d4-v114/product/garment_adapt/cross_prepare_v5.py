"""Apply compiled f32 Key arrays using the frozen strict import/restore core.

The caller proves their two-stage Basis-delta compilation. The old affine-only
result label is removed: this adapter certifies only actual array writeback.
"""
from .cross_prepare_v4 import CrossGarmentPreparation as StrictPreparation
from .contract import require


class CrossGarmentPreparation(StrictPreparation):
    def prepare(self, request, fitted_keys_world):
        result = super().prepare(request, fitted_keys_world)
        require(result.pop('same_declared_affine_all_keys_applied_f32', None) is True,
                'COMPILED_ARRAY_BASE_WRITEBACK_RECEIPT')
        result['all_compiled_key_arrays_applied_exact_f32'] = True
        result['geometry_method'] = 'Caller-pinned original finite fit plus one common per-vertex f32 delta on all Keys'
        return result
