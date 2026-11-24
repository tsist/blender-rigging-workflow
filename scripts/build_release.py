"""Build reviewed runtime, portable skill and source without collecting private data."""
from pathlib import Path
import argparse, ast, hashlib, json, re, shutil, zipfile
ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.40.1'
ADDON_NAME = 'rigging_workflow-0.40.1.zip'
CLI_PREVIEW = '0.42.3-agent-v003'
LOCAL_CUDA_OUTPUTS = {
    'cli-v003/rigging_workflow/volume/cuda/direct-pcg-sm120.cubin',
    'cli-v003/rigging_workflow/volume/cuda/manifest.json',
}

def sha(data):
    return hashlib.sha256(data).hexdigest()

def selected():
    publication = json.loads((ROOT/'PUBLICATION.json').read_text(encoding='utf8'))
    snapshot = publication['runtime_snapshot']
    expected = snapshot['files']
    addon_root = ROOT/'rigging_workflow'
    actual = {p.relative_to(addon_root).as_posix():sha(p.read_bytes())
              for p in addon_root.rglob('*') if p.is_file() and '__pycache__' not in p.parts}
    if actual != expected:
        raise ValueError('Runtime differs from the reviewed snapshot')
    manifest = json.loads((addon_root/'exchange-runtime-manifest.json').read_text(encoding='utf8'))
    if manifest['files'] != {n:s for n,s in actual.items() if n != 'exchange-runtime-manifest.json'}:
        raise ValueError('Runtime manifest differs from payload')
    path_files = set(snapshot['reviewed_workstation_path_files'])
    allowed = set(json.loads((ROOT/'DISTRIBUTION-FILES.json').read_text(encoding='utf8'))['files'])
    result = []
    seen = set()
    for p in ROOT.rglob('*'):
        rel = p.relative_to(ROOT)
        if not p.is_file() or any(x in ('.git','__pycache__','dist','test-output','runtime') for x in rel.parts) or p.suffix.lower() == '.log':
            continue
        name = rel.as_posix()
        if name in LOCAL_CUDA_OUTPUTS:
            continue
        if name not in allowed:
            raise ValueError('Unregistered distribution file: '+name)
        if p.is_symlink() or not p.resolve().is_relative_to(ROOT.resolve()):
            raise ValueError('External or symbolic distribution file: '+name)
        seen.add(name)
        data = p.read_bytes()
        if p.suffix.lower() in ('.pyc','.blend','.blend1','.npz','.exe','.dll','.cubin') or p.name in ('AGENTS.md','.env','secrets.json'):
            raise ValueError('Excluded file: '+name)
        if p.suffix.lower() == '.pmx':
            if name != 'rigging_workflow/resources/mmd/uka/MS_Uka.pmx' or sha(data) != expected['resources/mmd/uka/MS_Uka.pmx']:
                raise ValueError('Unknown binary resource: '+name)
        else:
            text = data.decode('utf-8-sig')
            if p.suffix == '.py':
                ast.parse(text,filename=name)
            if re.search(r'(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}|(?:AKIA|ASIA)[A-Z0-9]{16}|xox[baprs]-[A-Za-z0-9-]{12,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)',text):
                raise ValueError('Sensitive content: '+name)
            if re.search(r'(?<![A-Za-z0-9_])[A-Za-z]:[/\\]+|/(?:home|Users)/[A-Za-z0-9_.-]+/',text):
                package_rel = name.removeprefix('rigging_workflow/')
                if not name.startswith('rigging_workflow/') or package_rel not in path_files or sha(data) != expected[package_rel]:
                    raise ValueError('Unexpected workstation path: '+name)
        result.append((name,data))
    if seen != allowed:
        raise ValueError('Distribution inventory missing files: '+', '.join(sorted(allowed-seen)))
    return sorted(result)

def verify_archive(path,entries):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if len(names) != len(set(names)) or set(names) != {n for n,_ in entries}:
            raise ValueError('Archive member set differs: '+str(path))
        if z.testzip() is not None or any(z.read(n) != data for n,data in entries):
            raise ValueError('Archive bytes differ: '+str(path))

def archive(path,entries):
    if path.exists():
        raise FileExistsError(str(path))
    with zipfile.ZipFile(path,'x',compression=zipfile.ZIP_DEFLATED) as z:
        for name,data in sorted(entries):
            item = zipfile.ZipInfo(name,(2026,11,12,0,0,0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = 0o100644 << 16
            z.writestr(item,data)
    verify_archive(path,entries)
    return {'file':path.name,'bytes':path.stat().st_size,'sha256':sha(path.read_bytes())}

def main(out,accepted_addon=None):
    source = selected()
    out.mkdir(parents=True,exist_ok=True)
    addon = [(n,d) for n,d in source if n.startswith('rigging_workflow/')]
    prefix = 'skills/blender-rigging/'
    skill = [('blender-rigging/'+n[len(prefix):],d) for n,d in source if n.startswith(prefix)]
    skill.append(('blender-rigging/LICENSE',(ROOT/'LICENSE').read_bytes()))
    rows = [archive(out/('blender-rigging-workflow-'+CLI_PREVIEW+'-source.zip'),source),
            archive(out/('blender-rigging-skill-'+CLI_PREVIEW+'.zip'),skill)]
    if accepted_addon is None:
        rows.append(archive(out/ADDON_NAME,addon))
    else:
        expected = json.loads((ROOT/'PUBLICATION.json').read_text(encoding='utf8'))['runtime_snapshot']['accepted_zip_sha256']
        if sha(accepted_addon.read_bytes()) != expected:
            raise ValueError('Accepted ZIP identity differs')
        verify_archive(accepted_addon,addon)
        target = out/ADDON_NAME
        if target.exists():
            raise FileExistsError(str(target))
        shutil.copyfile(accepted_addon,target)
        rows.append({'file':target.name,'bytes':target.stat().st_size,'sha256':sha(target.read_bytes())})
    (out/'SHA256SUMS').write_text(''.join(r['sha256']+'  '+r['file']+'\n' for r in rows),encoding='utf8')
    print(json.dumps({'version':VERSION,'artifacts':rows},indent=2))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=ROOT/'dist')
    parser.add_argument('--accepted-addon',type=Path)
    args = parser.parse_args()
    main(args.output.resolve(),args.accepted_addon)
