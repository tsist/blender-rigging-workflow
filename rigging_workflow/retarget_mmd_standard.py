# SPDX-License-Identifier: GPL-3.0-or-later
"""Explicit Uka reference source; PMX resource remains CC-BY-4.0.

This is not the source model of arbitrary VMD motion. No parser or pose is
guessed here; the existing source backend imports and hashes the real data.
"""
import hashlib,json
from pathlib import Path

PMX_SHA256='6fbfff5cfb921e89df93d68a55dfa1549875d743a9d5e155272cb9737520d297'
PMX_NAME='MS_Uka.pmx'

def reference(resource_root=None):
    folder=Path(resource_root) if resource_root else Path(__file__).with_name('resources')/'mmd'/'uka'
    folder=folder.resolve(strict=True)
    manifest_path=folder/'manifest.json';attribution=folder/'ATTRIBUTION.md'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8-sig'))
    pmx=(folder/PMX_NAME).resolve(strict=True)
    if pmx.parent!=folder or hashlib.sha256(pmx.read_bytes()).hexdigest()!=PMX_SHA256:
        raise ValueError('标准Uka PMX身份不符；不猜测或替换来源')
    if manifest.get('sha256')!=PMX_SHA256 or manifest.get('license')!='CC BY 4.0' or not attribution.is_file() \
        or manifest.get('repository')!='https://github.com/mmdagent-ex/uka' \
        or manifest.get('commit')!='659f0d1a740fec68bb2817dc9c694a35e7a15f12' \
        or manifest.get('git_blob')!='548a5b8aae503dcdf850ab1ae29598cf44f01273':
        raise ValueError('标准参考源许可/署名sidecar缺失或不符')
    identity={'standard_reference':True,'source_mode':'STANDARD_REFERENCE','name':'CG-CA Uka V1.2',
        'author':manifest['author'],'license':'CC-BY-4.0','license_url':manifest['license_url'],
        'copyright':'CG-CA Uka (c) 2023-2024 by Nagoya Institute of Technology, Moonshot R&D Goal 1 Avatar Symbiotic Society',
        'repository':manifest['repository'],'commit':manifest['commit'],'git_blob':manifest['git_blob'],
        'source_url':manifest['source'],'pmx_file':PMX_NAME,'pmx_sha256':PMX_SHA256,
        'attribution_file':str(attribution),'manifest_file':str(manifest_path),
        'original_vmd_model_identified':False,'source_rest_or_proportions_guessed':False,
        'limitations':['这是明确标准参考源，不保证还原任意VMD原模型的rest/比例',
            '自定义骨、比例和通道差异依真实backend profile missing列表报告',
            '仅包含已核PMX与署名/来源sidecar；原贴图未获取，依赖未闭合']}
    return {'pmx':str(pmx),'batch_identity':identity,'expected_pmx_bones':302,'expected_pmx_vertices':49955,
        'resource_dependencies':[str(pmx),str(manifest_path),str(attribution)]}

def source_parameters(*,mode,pmx=None,resource_root=None,reference_acknowledged=False):
    if mode=='STANDARD_REFERENCE':
        if reference_acknowledged is not True:raise ValueError('请明确接受Uka标准参考源及原模型/比例不保证')
        return reference(resource_root)
    if mode!='EXPLICIT_PMX':raise ValueError('来源方式只能是显式PMX或标准参考源')
    path=Path(pmx or '')
    if not path.is_absolute() or not path.is_file() or path.suffix.lower()!='.pmx':
        raise ValueError('请明确选择存在的PMX绝对路径')
    return {'pmx':str(path.resolve(strict=True)),
        'batch_identity':{'source_mode':'EXPLICIT_PMX','standard_reference':False,
            'original_vmd_model_identified':False,'source_rest_or_proportions_guessed':False,
            'limitations':['使用明确选择的PMX；用户需核对它是否为动作的原模型','不自动推断原模型/rest/比例，不自动修正曲线']},
        'expected_pmx_bones':None,'expected_pmx_vertices':None,'resource_dependencies':[str(path.resolve())]}
