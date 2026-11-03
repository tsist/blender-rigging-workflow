"""Report exact compiled Key writes without the old affine-only label."""
from .cross_prepare_v4 import CrossGarmentPreparation as StrictPreparation
from .contract import require


class CrossGarmentPreparation(StrictPreparation):
    def prepare(self, request, fitted_keys_world):
        result = super().prepare(request, fitted_keys_world)
        require(result.pop('same_declared_affine_all_keys_applied_f32', None) is True,
                'COMPILED_ARRAY_BASE_WRITEBACK_RECEIPT', None)
        result['all_compiled_key_arrays_applied_exact_f32'] = True
        result['geometry_method'] = 'Caller-pinned finite fit plus common per-vertex f32 delta on all Keys'
        return result
