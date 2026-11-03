"""Strict original-ID preparation plus one explicit imported temporary Library.

The local append creates a zero-user donor Library with no inbound pointers.
It is declared import bookkeeping, removed before the prepared snapshot and
candidate write. No original ID or other Library is exempted or normalized.
"""
from copy import deepcopy
from pathlib import Path
import bpy
from .cross_prepare_v2 import CrossGarmentPreparation as StrictPreparation
from .cross_prepare import local_ids
from .contract import require
from .native import C


class CrossGarmentPreparation(StrictPreparation):
    def prepare(self,request,fitted_keys_world):
        result=super().prepare(request,fitted_keys_world)
        self.validate_prepared()
        libraries=[item for item in self.owned if item.bl_rna.identifier=='Library']
        require(len(libraries)==1,'CROSS_EXACT_TEMPORARY_DONOR_LIBRARY',len(libraries))
        library=libraries[0]
        require(library not in self.original_ids and Path(library.filepath).resolve()==Path(self.donor['file']).resolve(),
                'CROSS_LIBRARY_IMPORT_PROVENANCE',library.filepath)
        require(library.users==0 and not library.use_fake_user and not library.use_extra_user,
                'CROSS_TEMPORARY_LIBRARY_ACCOUNTING',{'users':library.users,'fake':library.use_fake_user,'extra':library.use_extra_user})
        consumers=bpy.data.user_map(subset={library}).get(library,set())
        require(not consumers,'CROSS_TEMPORARY_LIBRARY_HAS_NATIVE_CONSUMERS',
                [(v.bl_rna.identifier,v.name) for v in consumers])
        before=self._collect();identity={'type':'Library','name':library.name,'library':None}
        incoming=[e for e in before['author']['edges'] if e['target']==identity]
        outgoing=[e for e in before['author']['edges'] if e['user']==identity]
        require(not incoming and not outgoing,'CROSS_TEMPORARY_LIBRARY_HAS_OBSERVED_EDGES',{'incoming':incoming,'outgoing':outgoing})
        expected=deepcopy(before['author'])
        matches=[i for i,b in enumerate(expected['blocks']) if all(b.get(k)==v for k,v in identity.items())]
        require(len(matches)==1,'CROSS_TEMPORARY_LIBRARY_OBSERVER_ID',identity)
        removed_record=expected['blocks'].pop(matches[0])
        proof={'identity':identity,'donor':deepcopy(self.donor),'created_in_owned_append_graph':True,
               'before_native_users':library.users,'use_fake_user':library.use_fake_user,'use_extra_user':library.use_extra_user,
               'all_native_user_map_consumers':[],'all_observed_incoming_edges':incoming,'all_observed_outgoing_edges':outgoing,
               'policy':'Remove this single newly appended empty Library before freezing prepared state; no original or generic Library exemption',
               'removed_full_observer_record':removed_record}
        remaining_owned=self.owned-{library}
        bpy.data.batch_remove(ids=[library])
        self.owned=remaining_owned
        bpy.context.view_layer.update()
        after=self._collect()
        require(after['author']==expected,'CROSS_TEMPORARY_LIBRARY_ONLY_DECLARED_REMOVAL',C.diff(expected,after['author'],limit=20))
        for key in ('context','body_skin','bones'):
            if key=='context':equal=after[key]==before[key]
            else:equal=after[key].dtype==before[key].dtype and after[key].shape==before[key].shape and after[key].tobytes()==before[key].tobytes()
            require(equal,'CROSS_TEMPORARY_LIBRARY_NONAUTHOR_CHANGED',key)
        require(local_ids()-self.original_ids==self.owned,'CROSS_TEMPORARY_LIBRARY_OWNED_ID_GRAPH',None)
        protection=self._existing_authors(after['author'])
        self._pending['after']=after
        result.update(temporary_import_library_disposal=proof,protection=protection,author_sha256=C.digest(after['author']),
                      owned_ids=[{'type':i.bl_rna.identifier,'name':i.name} for i in self.owned])
        return result
