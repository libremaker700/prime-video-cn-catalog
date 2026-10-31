"""Identity-bound, reviewed lookup context for App titles outside the legacy cloud list."""
import argparse,copy,json,re
from pathlib import Path
from catalog import norm,write_json
ROOT=Path(__file__).resolve().parents[1]

def contextualize(targets, contexts):
    result=copy.deepcopy(targets)
    for target in result:
        for context in contexts.get('items',[]):
            if any(target.get(k)!=context.get(k) for k in ('officialId','kind','year','season')) or norm(target['originalTitle'])!=norm(context.get('originalTitle')):continue
            aliases=[context.get('displayTitle')]+context.get('reviewedAliases',[])+target.get('aliases',[])
            target['aliases']=list(dict.fromkeys(s for s in aliases if isinstance(s,str) and 0<len(s)<=100))[:8]
            target['displayTitle']=target['aliases'][0]
            target['lookup_title']=context.get('lookupTitle') or target['originalTitle']
            target['official_url']=context.get('officialUrl','')
            target['candidate_links']=context.get('candidateLinks',[])[:3]
    return result

def prepare(official_id,root=ROOT):
    if not re.fullmatch(r'[A-Z0-9]{10,26}',official_id):raise ValueError('Invalid official identity')
    targets=json.loads((root/'data/source-verification-targets.json').read_text(encoding='utf-8'))['items']
    contexts=json.loads((root/'data/title-lookup-context.json').read_text(encoding='utf-8'))
    found=[r for r in contextualize(targets,contexts) if r['officialId']==official_id]
    if len(found)!=1:raise ValueError('Unknown or ambiguous lookup identity')
    t=found[0]
    return {'schema':1,'items':[{'id':t['officialId'],'prime_id':t['officialId'],'kind':t['kind'],'year':t['year'],'season':t['season'],
        'english_title':t['originalTitle'],'original_title':t['originalTitle'],'display_name_zh':t['displayTitle'],
        'name_status':'pending_domestic_alias_review','lookup_title':t.get('lookup_title',t['originalTitle']),
        'official_url':t.get('official_url',''),'candidate_links':t.get('candidate_links',[])}]}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--official-id',required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    write_json(a.output,prepare(a.official_id));print('Prepared one identity-bound lookup; no playback data exported.')
