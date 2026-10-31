"""Run a bounded metadata batch for an exported App snapshot; no catalogue publication."""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from catalog import write_json
from title_lookup import Lookup,cache_key,identity,sanitize
from metadata_resolution import resolve,query,summary_key

ROOT=Path(__file__).resolve().parents[1]

def run(input_path,output_path,*,execute=False,title='',limit=12,monthly_limit=100,root=ROOT,client=None):
    payload=json.loads(Path(input_path).read_text(encoding='utf-8'))
    jobs=payload['items']
    if title:jobs=[r for r in jobs if title.casefold() in (r.get('english_title') or r.get('original_title') or '').casefold()]
    else:jobs=[r for r in jobs if not str(r.get('name_status','')).startswith('verified') or not r.get('synopsis_zh')]
    jobs=jobs[:max(0,min(int(limit),20))]
    if not execute:
        plan={'mode':'dry_run','jobs':[{'identity':identity(r),'season':r.get('season',0),'query':query(r,'tavily')}for r in jobs],
              'order':['tavily','douban','wikipedia','baidu'],'network_requests':0}
        write_json(Path(output_path),plan);return plan
    client=client or Lookup()
    path=root/'data/title-cache.json'
    cache=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'schema':1,'records':{},'budgets':{}}
    clock=datetime.now(timezone.utc);checked=clock.isoformat(timespec='seconds');month=clock.strftime('%Y-%m')
    used=int(cache.setdefault('budgets',{}).get(month,0));budget=max(0,min(monthly_limit,200))
    def reserve(slot):
        nonlocal used
        if used>=budget:return False
        used+=1;cache['budgets'][month]=used
        counters=cache.setdefault('key_usage',{}).setdefault(month,{})
        counters[str(slot)]=counters.get(str(slot),0)+1
        write_json(path,cache);return True
    result=[]
    for r in jobs:
        # Include season here: the legacy name cache is deliberately series-wide.
        key=cache_key(r)+':scope:'+summary_key(r)
        old=cache.setdefault('batch_records',{}).get(key,{})
        if old.get('identity')==identity(r) and (old.get('status')=='verified' or old.get('next_retry_at','')>checked):
            result.append(old);continue
        patch,evidence=resolve(client,r,checked,reserve)
        complete=patch and (patch.get('display_name_zh') or str(r.get('name_status','')).startswith('verified')) and (patch.get('synopsis_zh') or r.get('synopsis_zh'))
        row={'identity':identity(r),'season':r.get('season',0),'patch':patch or {},'evidence':evidence,
             'status':'verified' if complete else ('partial' if patch else 'pending_review'),'checked_at':checked,
             'next_retry_at':(clock+timedelta(days=7)).isoformat(timespec='seconds')}
        row=sanitize(row,client.keys);cache['batch_records'][key]=row;write_json(path,cache);result.append(row)
    out={'mode':'executed','items':result,'monthly_reserved':used,'publication':False}
    write_json(Path(output_path),out);return out

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--title',default='');p.add_argument('--limit',type=int,default=12)
    p.add_argument('--monthly-limit',type=int,default=100);p.add_argument('--execute',action='store_true')
    a=p.parse_args();result=run(a.input,a.output,execute=a.execute,title=a.title,limit=a.limit,monthly_limit=a.monthly_limit)
    print(json.dumps({'mode':result['mode'],'jobs':len(result.get('jobs',result.get('items',[]))),'publication':False}))
