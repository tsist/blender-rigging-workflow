"""Build source, unchanged base add-on and portable skill archives."""
from pathlib import Path
import argparse, ast, hashlib, json, re, zipfile
ROOT=Path(__file__).resolve().parents[1]
VERSION='0.1.0-dev1'
def selected():
    result=[]
    for p in ROOT.rglob('*'):
        if not p.is_file() or any(x in ('.git','__pycache__','dist','test-output') for x in p.relative_to(ROOT).parts):continue
        if p.suffix.lower() in ('.pyc','.blend','.blend1','.npz','.exe','.dll') or p.name in ('AGENTS.md','.env','secrets.json'):raise ValueError(str(p))
        text=p.read_text(encoding='utf-8-sig')
        if p.suffix=='.py':ast.parse(text,filename=str(p))
        if re.search(r'gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',text):raise ValueError('Sensitive content: '+str(p))
        if re.search(r'[FC]:[/\\]+(?:Users|Blender AI)',text):raise ValueError('Workstation path: '+str(p))
        result.append((p.relative_to(ROOT).as_posix(),p.read_bytes()))
    return sorted(result)
def archive(path,entries):
    if path.exists():raise FileExistsError(str(path))
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED) as z:
        for name,data in sorted(entries):
            item=zipfile.ZipInfo(name,(2026,11,3,0,0,0));item.compress_type=zipfile.ZIP_DEFLATED;item.external_attr=0o100644<<16
            z.writestr(item,data)
    with zipfile.ZipFile(path) as z:
        assert all(z.read(name)==data for name,data in entries)
    return {'file':path.name,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
def main(out):
    source=selected();out.mkdir(parents=True,exist_ok=True)
    addon='work/hrig-p2-v028/addon/';skill='skills/blender-rigging/'
    license=(ROOT/'LICENSE').read_bytes()
    bundles={'blender-rigging-workflow-'+VERSION+'-source.zip':source,
      'humanoid-rigging-probe-0.22.0.zip':[(n[len(addon):],d) for n,d in source if n.startswith(addon)]+[('humanoid_rigging_probe/LICENSE',license)],
      'blender-rigging-skill-'+VERSION+'.zip':[('blender-rigging/'+n[len(skill):],d) for n,d in source if n.startswith(skill)]+[('LICENSE',license)]}
    rows=[archive(out/name,entries) for name,entries in bundles.items()]
    (out/'SHA256SUMS').write_text(''.join(r['sha256']+'  '+r['file']+'\n' for r in rows),encoding='utf8')
    print(json.dumps({'artifacts':rows},indent=2))
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,default=ROOT/'dist')
    main(parser.parse_args().output.resolve())
