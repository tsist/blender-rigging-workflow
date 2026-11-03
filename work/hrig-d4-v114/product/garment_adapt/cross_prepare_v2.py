"""Same native preparation with strict v2 original-ID user-accounting proof.

The existing native orchestration is unchanged. The old users normalization is
fully replaced by this pure proof, never called by the v2 implementation.
"""
from .cross_prepare import CrossGarmentPreparation as BasePreparation
from .reference_contract import prove_originals


class CrossGarmentPreparation(BasePreparation):
    def _existing_authors(self,after):
        return prove_originals(self.before['author'],after,self.before['context']['scene'],
            self.garment.name,self.garment.data.name,self.garment.data.shape_keys.name,self.rig.name)
