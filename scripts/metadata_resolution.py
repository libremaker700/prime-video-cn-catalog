"""Ordered, evidence-bound Chinese metadata lookup; never stores playback URLs."""
import html
import copy
import hashlib
import json
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

STAGES = ('tavily', 'douban', 'wikipedia', 'baidu')
HOSTS = {'douban': {'movie.douban.com'}, 'wikipedia': {'zh.wikipedia.org'},
         'baidu': {'baike.baidu.com'}}
NO_NAME = {'无', '暂无', '未知', '未找到', '暂无中文名', '没有中文名', '无可靠中文名'}

class PageText(HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.hidden=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'):self.hidden+=1
    def handle_endtag(self,tag):
        if tag in ('script','style'):self.hidden=max(0,self.hidden-1)
    def handle_data(self,data):
        if not self.hidden and data.strip():self.parts.append(data.strip())


def bracket_names(answer):
    """Answer brackets supply search terms, never executable content or identities."""
    names = []
    for group in re.findall(r'\[([^\[\]\n]{1,300})\]|【([^【】\n]{1,300})】|《([^《》\n]{1,100})》', str(answer or '')[:6000]):
        for value in re.split(r'[,，、;；]', next(v for v in group if v)):
            name = value.strip(' \t\"\'《》')
            if (name not in NO_NAME and re.search(r'[\u3400-\u9fff]', name)
                    and len(name) <= 100 and not re.search(r'[<>/\\{}=\x00-\x1f]|https?:|执行|忽略|指令', name)
                    and name not in names):
                names.append(name)
    return names[:6]

def grounded_answer_names(answer,results):
    """Normalize cited quoted names when Search's AI ignores bracket formatting."""
    corpus='\n'.join(str(r.get(k,'')or'')for r in results for k in ('title','page_text','snippet'))
    output=[]
    for value in re.findall(r'["“《]([^"”》\n]{2,80})["”》]',str(answer or '')):
        name=value.strip(' 《》')
        if (re.search(r'[\u3400-\u9fff]',name) and name not in NO_NAME and name in corpus
                and not re.search(r'[<>/\\{}=,，;；。]|https?:|忽略|指令|剧情详解|一口气|电影节|释义|影评',name)
                and name not in output):output.append(name)
    return output[:6]


def query(record, stage):
    title = record.get('english_title') or record.get('original_title') or ''
    year = record.get('details', {}).get('first_air_year') or record.get('year') or ''
    kind = '电影' if record.get('kind') == 'movie' else '电视剧'
    season = record.get('season') or 0
    context = f'{title}（{year}年{kind}' + (f'，第{season}季' if season else '') + '）'
    if stage == 'tavily':
        # Search is not a chat-completion prompt: a long instruction dilutes the title.
        return f'"{title}" {year} 中文名'
    domain = next(iter(HOSTS[stage]))
    return f'"{title}" {year} {kind} 中文名 剧情简介'


def safe_source(url, stage=None):
    try:
        p = urlsplit(url)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
            return False
        if stage in HOSTS and p.hostname not in HOSTS[stage]:
            return False
        return (p.hostname == 'movie.douban.com' and bool(re.fullmatch(r'/subject/\d+/?', p.path))
                or p.hostname == 'zh.wikipedia.org' and p.path.startswith('/wiki/')
                or p.hostname == 'baike.baidu.com' and p.path.startswith('/item/'))
    except (TypeError, ValueError):
        return False

def retrieved_page_patch(record,item,checked_at):
    """Validate retrieved encyclopedia page text, distinct from AI prose/snippets."""
    from title_lookup import mentions_original,year,source
    url=item.get('url','');text=item.get('page_text','')
    if not safe_source(url) or not text:return None
    expected=year(record)
    originals=[record.get(k) for k in ('english_title','original_title') if record.get(k)]
    definitions=list(re.finditer(r'《([^》\n]{1,100})》[（(]([^）)\n]{1,200})[）)]',text))
    for hit in definitions:
        name=hit[1];intro=text[max(0,hit.start()-150):hit.start()+1200]
        from source_matching import release_years,with_release_evidence
        if not expected or not any(str(y) in intro for y in release_years(with_release_evidence(record))) or not any(mentions_original(t,hit[2])for t in originals):continue
        kind_words=('电影','電影','影片') if record.get('kind')=='movie' else ('电视剧','電視劇','影集','剧集','情景喜剧')
        if not any(k in intro[:650] for k in kind_words):continue
        if not re.search(r'[\u3400-\u9fff]',name):continue
        # A season-specific page must explicitly identify the requested season.
        season=int(record.get('season')or 0)
        if season>1 and not re.search(r'(?:第\s*'+str(season)+r'\s*季|Season\s*'+str(season)+r'\b)',intro,re.I):continue
        return {'display_name_zh':name,'aliases_zh':[name],'name_status':'verified_title_sources',
            'name_checked_at':checked_at,'sources':[source('tavily_retrieved_encyclopedia_page',url,checked_at)],
            'name_evidence_sha256':hashlib.sha256(text.encode()).hexdigest()}
    return None


def page_patch(record, content, url, checked_at):
    """Only structured, same-entity film/series metadata may become a patch."""
    from title_lookup import mentions_original, year, source
    if not safe_source(url) or len(content) > 2_000_000:
        return None
    expected = year(record)
    originals = [record.get(k) for k in ('english_title', 'original_title') if record.get(k)]
    nodes = []
    for raw in re.findall(r'<script\b[^>]*type=[\"\']application/ld\+json[\"\'][^>]*>(.*?)</script>', content, re.S | re.I):
        try:
            value = json.loads(html.unescape(raw))
            values = value if isinstance(value, list) else [value]
            for node in values:
                if isinstance(node, dict):
                    nodes.extend(node.get('@graph', [node]))
        except (ValueError, TypeError):
            continue
    # Baidu pages usually expose labelled basic-info rather than JSON-LD.
    if urlsplit(url).hostname == 'baike.baidu.com' and not nodes:
        parser=PageText();parser.feed(content);visible='\n'.join(parser.parts)
        title=re.search(r'<h1\b[^>]*>(.*?)</h1>',content,re.S|re.I)
        original=re.search(r'(?:外文名|外文名称)\s*[:：]?\s*([^\n]{1,160})',visible)
        date=re.search(r'(?:首播时间|首播日期|上映时间|上映日期)\s*[:：]?\s*((?:19|20)\d{2}[^\n]{0,30})',visible)
        kind=re.search(r'(?:作品类型|类型)\s*[:：]?\s*([^\n]{1,100})',visible)
        if title and original and date and kind:
            tv=bool(re.search(r'电视剧|電視劇|剧集|情景喜剧',kind[1]))
            film=bool(re.search(r'电影|電影',kind[1]))
            if tv != film:
                description=re.search(r'<meta\b(?=[^>]*name=[\"\']description[\"\'])[^>]*content=[\"\']([^\"\']*)',content,re.I)
                nodes=[{'@type':'TVSeries' if tv else 'Movie','name':html.unescape(re.sub('<[^>]*>','',title[1])),
                        'alternateName':original[1],'datePublished':date[1],
                        'description':html.unescape(description[1]) if description else ''}]
    valid = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        kind = node.get('@type')
        if kind not in (['Movie'] if record.get('kind') == 'movie' else ['TVSeries', 'TVSeason']):
            continue
        if kind == 'TVSeason' and record.get('season') and str(node.get('seasonNumber')) != str(record['season']):
            continue
        name = str(node.get('name') or '')
        aliases = str(node.get('alternateName') or '')
        date = str(node.get('datePublished') or node.get('startDate') or '')
        # Identity must be in title fields, never an unrelated mention in the plot.
        from source_matching import release_years,with_release_evidence
        if not expected or not any(date[:4] == str(y) for y in release_years(with_release_evidence(record))) or not any(mentions_original(t, name+' '+aliases) for t in originals):
            continue
        display=name
        for original in originals:
            display=re.sub(r'\s*[（(]?'+re.escape(original)+r'[）)]?\s*$','',display,flags=re.I)
        display=display.strip()
        if not re.search(r'[\u3400-\u9fff]', display) or len(display) > 100:
            continue
        patch = {'display_name_zh': display, 'aliases_zh': [display], 'name_status': 'verified_title_sources',
                 'name_checked_at': checked_at, 'sources': [source('verified_chinese_page', url, checked_at)]}
        summary = html.unescape(re.sub('<[^>]+>', ' ', str(node.get('description') or ''))).strip()
        season = int(record.get('season') or 0)
        # A series synopsis is not evidence for a particular later season.
        can_summarize = not season or (kind == 'TVSeason' and str(node.get('seasonNumber')) == str(season))
        if can_summarize and len(re.findall(r'[\u3400-\u9fff]', summary)) >= 12 and len(re.findall('[A-Za-z]', summary)) < 2*len(re.findall(r'[\u3400-\u9fff]', summary)):
            patch.update(synopsis_zh=summary[:5000], synopsis_status='ok', synopsis_source_type='verified_chinese_page', synopsis_source_url=url)
        valid.append(patch)
    return valid[0] if len(valid) == 1 else None


def resolve(client, record, checked_at, reserve=None, source_probe=None):
    """Probe candidates in order. A failed probe never becomes permanent no-source."""
    from catalog import merge
    combined, history, candidates = {}, [], []
    probed=set()
    probe_state = 'not_checked'
    for stage in STAGES:
        if stage == 'wikipedia':
            patch = client.direct(record, checked_at)
            evidence = {'stage': stage, 'status': 'verified' if patch else 'not_found', 'results': []}
        elif client.key and (not client.stopped or (hasattr(client,'has_cached') and client.has_cached(record,stage))):
            patch, evidence = client.search(record, checked_at, reserve, stage=stage)
        else:
            patch, evidence = None, {'stage': stage, 'status': 'unavailable', 'results': []}
        history.append(evidence)
        names = evidence.get('candidate_names', []) + (patch or {}).get('aliases_zh', [])
        for name in names:
            if name not in candidates:
                candidates.append(name)
            verified_aliases=(patch or {}).get('aliases_zh',[])
            proof_key=(name,name in verified_aliases)
            if proof_key not in probed:
                probed.add(proof_key)
                if source_probe and probe_state != 'matched':
                    # The caller validates returned title/year/type/season before binding.
                    try:
                        state = source_probe(record|{'lookup_verified_aliases':verified_aliases}, name)
                        probe_state = state if state in ('matched', 'unmatched', 'unavailable', 'ambiguous') else 'unavailable'
                    except Exception:
                        probe_state = 'unavailable'
        if patch:
            combined = merge(combined, patch)
        # Source resolution is complete once the caller confirms a unique item
        # with episode routes. Synopsis enrichment runs as a separate job.
        if source_probe is not None and probe_state == 'matched':
            break
        name_done = combined.get('display_name_zh')
        if str(record.get('name_status','')).startswith('verified'):
            name_done = name_done or record.get('display_name_zh')
        summary_done = combined.get('synopsis_zh') or record.get('synopsis_zh')
        if name_done and summary_done and (source_probe is None or probe_state == 'matched'):
            break
    return combined or None, {'stage_order': list(STAGES), 'stages': history, 'candidate_names': candidates[:12],
                              'playback_status': probe_state, 'retryable': probe_state != 'matched',
                              'status': 'verified' if combined else 'pending_review'}

SUMMARY_FIELDS = {'synopsis_zh','synopsis_status','synopsis_source_type','synopsis_source_url'}

def summary_identity(record):
    from title_lookup import identity
    return identity(record) | {'season':int(record.get('season') or 0),
        'original_sha256':hashlib.sha256(str(record.get('synopsis_original') or '').encode()).hexdigest()}

def summary_key(record):
    return hashlib.sha256(json.dumps(summary_identity(record),sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def remember_summary(record,patch,cache,checked_at):
    from sources import chinese
    if record.get('kind')!='movie' and not record.get('season'):return
    safe={k:v for k,v in patch.items() if k in SUMMARY_FIELDS}
    if chinese(safe.get('synopsis_zh')) and safe.get('synopsis_source_url'):
        cache.setdefault('summary_records',{})[summary_key(record)]={'identity':summary_identity(record),
            'status':'verified','patch':safe,'checked_at':checked_at}

def summary_targets(record):
    if record.get('kind') == 'movie':
        return [(record,record)]
    result=[]
    for season in record.get('seasons',[]):
        number=season.get('season')
        target={**record,'id':str(record['id'])+':s'+str(number),'season':number,
                'prime_id':season.get('official_id') or season.get('official_compact_id'),
                'year':season.get('year') or (record.get('year') if number==1 else None),
                'details':{},'synopsis_original':season.get('synopsis_original',''),
                'synopsis_zh':season.get('synopsis_zh','')}
        result.append((season,target))
    return result

def apply_summary_cache(record,cache):
    from sources import chinese
    out=copy.deepcopy(record)
    for into,target in summary_targets(out):
        entry=cache.get('summary_records',{}).get(summary_key(target),{})
        if chinese(into.get('synopsis_zh')) or entry.get('identity')!=summary_identity(target):continue
        patch={k:v for k,v in entry.get('patch',{}).items() if k in SUMMARY_FIELDS}
        if entry.get('status')=='verified' and chinese(patch.get('synopsis_zh')) and patch.get('synopsis_source_url'):
            into.update(patch)
    return out

def enrich_summaries(items,cache,client,checked_at,reserve,limit,cache_path):
    from sources import chinese
    from catalog import write_json
    from title_lookup import year,sanitize
    report={'attempted':0,'verified':0,'pending':0,'cache_hits':0}
    updated=[]
    for original in items:
        out=apply_summary_cache(original,cache)
        if out!=original:report['cache_hits']+=1
        for into,target in summary_targets(out):
            if chinese(into.get('synopsis_zh')):continue
            if report['attempted']>=limit or not year(target) or not target.get('prime_id'):
                report['pending']+=1;continue
            key=summary_key(target)
            old=cache.get('summary_records',{}).get(key,{})
            if old.get('next_retry_at','')>checked_at:continue
            report['attempted']+=1
            patch,evidence=client.resolve(target,checked_at,reserve)
            safe={k:v for k,v in (patch or {}).items() if k in SUMMARY_FIELDS}
            valid=chinese(safe.get('synopsis_zh')) and bool(safe.get('synopsis_source_url'))
            from datetime import datetime,timedelta
            retry=(datetime.fromisoformat(checked_at)+timedelta(days=7)).isoformat(timespec='seconds')
            cache.setdefault('summary_records',{})[key]=sanitize({'identity':summary_identity(target),
                'status':'verified' if valid else 'pending_review','patch':safe if valid else {},
                'checked_at':checked_at,'next_retry_at':retry,'evidence':evidence},getattr(client,'keys',[]))
            write_json(cache_path,cache)
            if valid:into.update(safe);report['verified']+=1
            else:report['pending']+=1
        updated.append(out)
    return updated,report
