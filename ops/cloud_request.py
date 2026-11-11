"""Validate a cloud request without using any Google or LINE credentials."""
import json,os,re,subprocess
from pathlib import Path

def validate(data,default_sha):
    mode=data.get('mode','check')
    if mode not in ('check','deploy','report-check','report-deploy'):raise ValueError('Invalid mode')
    sha=data.get('source_sha') or (default_sha if mode in ('check','report-check') else '')
    if not re.fullmatch('[0-9a-f]{40}',sha):raise ValueError('A full source commit SHA is required')
    previous=data.get('expected_revision','')
    if mode=='deploy' and not re.fullmatch('line-bot-calc-[a-z0-9-]{1,49}',previous):
        raise ValueError('Expected current revision is required for deployment')
    if mode=='report-deploy' and not re.fullmatch('report-generation-[1-9][0-9]*',previous):
        raise ValueError('Expected report job generation is required for deployment')
    return dict(mode=mode,source_sha=sha,expected_revision=previous)

if __name__=='__main__':
    if os.environ.get('GITHUB_EVENT_NAME')=='workflow_dispatch':
        data={k:os.environ.get('INPUT_'+k.upper(),'') for k in ('mode','source_sha','expected_revision')}
    else:data=json.loads(Path('ops/cloud-request.json').read_text(encoding='utf-8'))
    result=validate(data,os.environ['GITHUB_SHA'])
    subprocess.run(['git','merge-base','--is-ancestor',result['source_sha'],'HEAD'],check=True)
    with open(os.environ['GITHUB_OUTPUT'],'a',encoding='utf-8') as f:
        for k,v in result.items():f.write(k+'='+v+'\n')
    print('REQUEST_VALIDATED',result['mode'],result['source_sha'])
