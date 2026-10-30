import contextlib
import copy
import io
import json
import sys
import tempfile
import unittest
from unittest.mock import Mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from catalog import write_json
from title_lookup import apply_cache, bangumi_patch, cache_key, identity, run, sanitize, wiki_patch, mentions_original, Lookup

CLOCK = datetime(2026, 10, 29, 16, tzinfo=timezone.utc)


def record(n=1):
    return {'id': f'seed:{n}', 'slug': f'title-{n}', 'original_title': f'Example {n}', 'english_title': f'Example {n}',
            'kind': 'tv', 'year': 2020, 'name_status': 'pending_review', 'display_name_zh': f'Example {n}',
            'identity_status': 'pending_review', 'seasons': [{'season': 2, 'synopsis_zh': '第二季', 'poster': None}]}


def patch_for(r):
    return {'display_name_zh': '测试作品', 'aliases_zh': ['测试作品'], 'name_status': 'verified_title_sources',
            'sources': [{'type': 'wikipedia_title', 'url': 'https://zh.wikipedia.org/wiki/测试作品'}]}


class Fake:
    key = 'private-key-for-test'
    stopped = False
    def __init__(self, direct=False, fail=False):
        self.direct_calls = 0
        self.search_calls = 0
        self.direct_success = direct
        self.fail = fail
    def direct(self, r, checked):
        self.direct_calls += 1
        return patch_for(r) if self.direct_success else None
    def search(self, r, checked):
        self.search_calls += 1
        if self.fail:
            self.stopped = True
            return None, {'error': 'HTTP 429'}
        return patch_for(r), {'results': [{'title': '测试作品', 'url': 'https://zh.wikipedia.org/wiki/测试作品'}]}
    def verify_results(self, r, results, checked):
        return patch_for(r)


class TitlesTests(unittest.TestCase):
    def setup_repo(self, directory, items):
        root=Path(directory)
        write_json(root/'docs/catalog.json', {'schema':'prime-video-cn-catalog/1', 'item_count':len(items), 'items':items})
        write_json(root/'docs/version.json', {'last_success_at':'2026-10-01T00:00:00+00:00'})
        return root

    def run_lookup(self, root, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return run(root, clock=CLOCK, **kwargs)

    def test_search_once_then_cache_survives_new_process_and_manual_force(self):
        with tempfile.TemporaryDirectory() as directory:
            original=record()
            root=self.setup_repo(directory,[original])
            first=Fake()
            result=self.run_lookup(root,client=first)
            self.assertEqual(result['searched'],1)
            self.assertEqual(first.search_calls,1)
            item=json.loads((root/'docs/catalog.json').read_bytes())['items'][0]
            self.assertEqual(item['seasons'],original['seasons'])
            self.assertEqual(item['id'],original['id'])
            self.assertEqual(json.loads((root/'docs/version.json').read_bytes())['last_success_at'],'2026-10-01T00:00:00+00:00')
            # Even if catalogue names are restored to an old snapshot, cache reapplies.
            write_json(root/'docs/catalog.json',{'schema':'prime-video-cn-catalog/1','item_count':1,'items':[original]})
            second=Fake()
            again=self.run_lookup(root,client=second,force=True)
            self.assertEqual(again['cache_hits'],1)
            self.assertEqual((second.search_calls,second.direct_calls),(0,0))

    def test_verified_and_user_names_never_search_even_manual(self):
        with tempfile.TemporaryDirectory() as directory:
            items=[record(1),record(2)]
            items[0].update(display_name_zh='用户名称',name_status='user_app_preference')
            items[1].update(display_name_zh='已核验',name_status='verified')
            root=self.setup_repo(directory,items)
            client=Fake()
            report=self.run_lookup(root,client=client,force=True)
            self.assertEqual(report['skipped_verified'],2)
            self.assertEqual((client.direct_calls,client.search_calls),(0,0))

    def test_budget_and_throttle_do_not_overwrite_existing_or_retry(self):
        for fail in (False,True):
            with tempfile.TemporaryDirectory() as directory:
                items=[record(1),record(2),record(3)]
                root=self.setup_repo(directory,items)
                client=Fake(fail=fail)
                report=self.run_lookup(root,client=client,monthly_limit=1)
                self.assertEqual(client.search_calls,1)
                self.assertEqual(report['monthly_reserved'],1)
                if fail:
                    self.assertEqual(json.loads((root/'docs/catalog.json').read_bytes())['items'],items)
                    follow=Fake()
                    self.run_lookup(root,client=follow,force=True)
                    self.assertEqual(follow.search_calls,0)

    def test_weekly_due_makes_no_network_or_file_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root=self.setup_repo(directory,[record()])
            self.run_lookup(root,client=Fake())
            before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
            client=Fake()
            self.assertIsNone(self.run_lookup(root,client=client))
            self.assertEqual(client.search_calls,0)
            self.assertEqual(before,{str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_cache_fingerprint_rejects_changed_identity_and_ignores_extra_fields(self):
        r=record()
        malicious=patch_for(r)|{'prime_id':'WRONG','seasons':[],'poster':{'path':'wrong'},'synopsis_zh':'串季'}
        cache={'records':{cache_key(r):{'status':'verified','identity':identity(r),'patch':malicious}}}
        out=apply_cache(r,cache)
        for field in ('id','seasons','synopsis_zh','poster','prime_id'):
            self.assertEqual(out.get(field),r.get(field))
        for field,value in [('year',2021),('kind','movie'),('id','seed:other')]:
            changed=r|{field:value}
            self.assertEqual(apply_cache(changed,cache),changed)
        changed=copy.deepcopy(r)
        changed['seasons'].append({'season':3})
        self.assertEqual(apply_cache(changed,cache)['display_name_zh'],'测试作品')

    def test_wiki_definition_requires_original_year_and_media(self):
        r=record()
        good={'extract':'《测试作品》（英语：Example 1）是2020年首播的电视剧。','fullurl':'https://zh.wikipedia.org/wiki/测试作品'}
        self.assertIsNotNone(wiki_patch(r,good,'now'))
        for text in ['《测试作品》（英语：Example 1）是2021年首播的电视剧。','《测试作品》（英语：Example 1）是2020年发行的电影。','《测试作品》（英语：Other）是2020年首播的电视剧。']:
            self.assertIsNone(wiki_patch(r,good|{'extract':text},'now'))
        r.update(display_name_zh='其他译名')
        self.assertIsNone(wiki_patch(r,good,'now'))

    def test_short_english_names_cannot_match_inside_another_title(self):
        self.assertFalse(mentions_original('Up','《作品》（Super）是2020年电影。'))
        self.assertFalse(mentions_original('Example 1','Example 10'))
        self.assertTrue(mentions_original("Tom Clancy's Jack Ryan",'Tom Clancy’s Jack Ryan'))

    def test_anime_exact_original_and_first_air_season_not_manga(self):
        r=record()|{'kind':'anime','original_title':'ヴィンランド・サガ','english_title':'Vinland Saga','year':2019}
        subject={'id':240386,'type':2,'platform':'TV','date':'2019-07-07','name':'ヴィンランド・サガ','name_cn':'冰海战记'}
        self.assertIsNotNone(bangumi_patch(r,subject,'now'))
        for change in ({'type':1},{'date':'2023-01-09'},{'name':'ヴィンランド・サガ SEASON 2'},{'platform':'剧场版'}):
            self.assertIsNone(bangumi_patch(r,subject|change,'now'))

    def test_direct_lookup_avoids_search_and_secret_is_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            root=self.setup_repo(directory,[record()])
            client=Fake(direct=True)
            result=self.run_lookup(root,client=client)
            self.assertEqual(result['direct_verified'],1)
            self.assertEqual(client.search_calls,0)
            self.assertNotIn(client.key,json.dumps(sanitize({'text':client.key},client.key)))

    def test_review_saved_evidence_verifies_without_another_search(self):
        with tempfile.TemporaryDirectory() as directory:
            r=record()
            root=self.setup_repo(directory,[r])
            write_json(root/'data/title-cache.json',{'schema':1,'records':{cache_key(r):{
                'identity':identity(r),'status':'pending_review','next_retry_at':'2027-01-01T00:00:00+00:00',
                'evidence':{'results':[{'url':'https://zh.wikipedia.org/wiki/测试作品'}]}}},'budgets':{'2026-10':4}})
            client=Fake()
            report=self.run_lookup(root,client=client,force=True,limit=0,review_saved=True)
            self.assertEqual((client.search_calls,client.direct_calls),(0,0))
            self.assertEqual(report['monthly_reserved'],4)
            self.assertEqual(report['cache_hits'],1)

    def test_expired_pending_entry_rechecks_saved_source_before_searching(self):
        with tempfile.TemporaryDirectory() as directory:
            r=record()
            root=self.setup_repo(directory,[r])
            write_json(root/'data/title-cache.json',{'schema':1,'records':{cache_key(r):{
                'identity':identity(r),'status':'pending_review','next_retry_at':'2026-10-01T00:00:00+00:00',
                'evidence':{'results':[{'url':'https://zh.wikipedia.org/wiki/测试作品'}]}}},'budgets':{}})
            client=Fake()
            report=self.run_lookup(root,client=client)
            self.assertEqual((client.search_calls,client.direct_calls),(0,0))
            self.assertEqual(report['cache_hits'],1)

    def test_sitcom_is_a_tv_definition_not_a_movie(self):
        page={'extract':'《测试作品》（英语：Example 1）是一部2020年首播的美国情境喜剧。','fullurl':'https://zh.wikipedia.org/wiki/测试作品'}
        self.assertIsNotNone(wiki_patch(record(),page,'now'))
        self.assertIsNone(wiki_patch(record()|{'kind':'movie'},page,'now'))

    def test_search_snippet_cannot_replace_source_definition(self):
        definition='《测试作品》（英语：Example 1）是一部2020年首播的电视剧。'
        for raw in (None,definition):
            session=Mock()
            session.post.return_value.status_code=200
            session.post.return_value.json.return_value={'usage':{'credits':1},'results':[{
                'title':'测试作品','url':'https://zh.wikipedia.org/wiki/测试作品','content':definition,'raw_content':raw}]}
            session.get.return_value.status_code=503
            client=Lookup('private-test-key',session)
            patch,evidence=client.search(record(),'now')
            self.assertEqual(bool(patch),bool(raw))
            self.assertEqual(session.post.call_count,1)
            self.assertEqual(session.post.call_args.kwargs['json']['search_depth'],'basic')
            self.assertFalse(session.post.call_args.kwargs['json']['auto_parameters'])

    def test_two_keys_failover_reserves_each_request_and_stays_on_backup(self):
        session=Mock();bad=Mock(status_code=432);good=Mock(status_code=200)
        good.json.return_value={'results':[]};session.post.side_effect=[bad,good,good]
        client=Lookup(session=session,keys=['first-private-key','second-private-key']);slots=[]
        reserve=lambda slot:slots.append(slot) or True
        client.search_with_budget(record(),'now',reserve);client.search_with_budget(record(2),'now',reserve)
        self.assertEqual(slots,[1,2,2]);self.assertEqual(client.requests,3);self.assertEqual(client.failovers,1)
        self.assertEqual([c.kwargs['headers']['Authorization'] for c in session.post.call_args_list],['Bearer first-private-key','Bearer second-private-key','Bearer second-private-key'])

    def test_budget_prevents_an_unreserved_backup_request(self):
        session=Mock();session.post.return_value.status_code=401
        client=Lookup(session=session,keys=['first-private-key','second-private-key'])
        _,evidence=client.search_with_budget(record(),'now',lambda slot:slot==1)
        self.assertEqual(session.post.call_count,1);self.assertEqual(evidence['error'],'local_budget_exhausted')

    def test_rate_limits_and_uncertain_timeouts_do_not_cycle_keys(self):
        session=Mock();session.post.return_value.status_code=429
        client=Lookup(session=session,keys=['first-private-key','second-private-key'])
        client.search(record(),'now');self.assertEqual(session.post.call_count,1);self.assertTrue(client.stopped)
        import requests
        session=Mock();session.post.side_effect=requests.Timeout('transport detail')
        client=Lookup(session=session,keys=['first-private-key','second-private-key'])
        _,evidence=client.search(record(),'now');self.assertEqual(session.post.call_count,1);self.assertNotIn('transport detail',str(evidence))

    def test_both_keys_are_redacted(self):
        out=sanitize({'a':'first-private-key','b':'second-private-key'},['first-private-key','second-private-key'])
        self.assertEqual(out,{'a':'[REDACTED]','b':'[REDACTED]'})


if __name__=='__main__':
    unittest.main()
