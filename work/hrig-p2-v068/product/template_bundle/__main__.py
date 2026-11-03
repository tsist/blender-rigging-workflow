"""python -m template_bundle COMMAND --request PATH

Requests are strict JSON. No installation, network access, Python/RNA replay,
Blender registration or implicit overwrite is performed by this entry point.
"""
import argparse
import json
import sys
from . import bundle as B


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('export', 'inspect', 'instantiate', 'override', 'upgrade', 'compile'))
    parser.add_argument('--request', required=True)
    args = parser.parse_args()
    try:
        r = B.parse(B.read(args.request))
        if args.command == 'export':
            B.exact(r, ('spec', 'destination'), 'request')
            out = B.export(r['spec'], r['destination'])
            out = {'reference': out['reference'], 'manifest': out['manifest']}
        elif args.command == 'inspect':
            B.exact(r, ('directory', 'manifest_sha256'), 'request')
            data = B.inspect(r['directory'], r['manifest_sha256'])
            out = {'reference': data['reference'], 'manifest': data['manifest'], 'roles': len(data['semantics']['roles']), 'expressions': list(data['semantics']['expressions']), 'meshes': len(data['inventory']['meshes']), 'distribution': data['provenance']['distribution'],
                   'modules': {'semantic_roles': 'available_explicit_mapping', 'native_controls': 'pinned_profile_requires_native_backend',
                               'facial_semantics': 'available_existing_key_references', 'weights': 'descriptor_only_requires_transfer_backend',
                               'skin_deformation': 'not_applied_by_template_layer', 'structure_migration': 'unsupported_explicit_reinstantiate_required'}}
        elif args.command == 'instantiate':
            B.exact(r, ('source', 'target', 'mapping', 'destination'), 'request')
            out = B.instantiate(r['source'], r['target'], r['mapping'], r['destination'])
        elif args.command == 'override':
            B.exact(r, ('file', 'expected_sha256', 'edits'), 'request')
            out = B.override(r['file'], r['expected_sha256'], r['edits'])
        elif args.command == 'upgrade':
            B.exact(r, ('file', 'expected_sha256', 'incoming'), 'request')
            out = B.upgrade(r['file'], r['expected_sha256'], r['incoming'])
        else:
            B.exact(r, ('instance', 'target', 'role_requests', 'defaults'), 'request')
            instance = B.load_descriptor(r['instance'])
            B.instance_check(instance)
            target = B._ref(r['target'])
            facts = B.load_descriptor(r['defaults'])
            B.require(type(facts) is dict and facts.get('character') == target['profile']['character_id'] and type(facts.get('defaults')) is dict, 'DEFAULT_CHARACTER_IDENTITY')
            out = B.compile_batch(instance, target, r['role_requests'], facts['defaults'])
        print(json.dumps({'ok': True, 'data': out, 'error': None}, ensure_ascii=False, allow_nan=False))
    except (B.BundleError, OSError, KeyError, TypeError, ValueError) as error:
        print(json.dumps({'ok': False, 'data': None, 'error': {'code': getattr(error, 'code', 'IO_OR_VALUE'), 'details': getattr(error, 'details', {'message': str(error)})}}, ensure_ascii=False))
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
