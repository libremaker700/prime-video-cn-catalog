import copy
import json
import sys
import tempfile
import contextlib
import io
from datetime import datetime, timezone
import unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from metadata_resolution import (bracket_names,resolve,page_patch,query,summary_identity,summary_key,
    apply_summary_cache,enrich_summaries,safe_source,retrieved_page_patch,grounded_answer_names)
from title_lookup import Lookup
from sources import chinese

def record(**extra):
    return {'id':'prime:ONE','prime_id':'B012345678','kind':'tv','year':2026,
        'english_title':'Eternally Yours','original_title':'Eternally Yours',
        'display_name_zh':'Eternally Yours','name_status':'pending_review',**extra}

def page(kind='TVSeries',year=2026,season=None):
    node={'@type':kind,'name':'永远与你 Eternally Yours','datePublished':str(year)+'-10-08',
          'description':'一对相伴五百年的吸血鬼夫妇，在家庭与婚姻中面对新的挑战。'}
    if season:node['seasonNumber']=season
    return '<script type="application/ld+json">'+json.dumps(node,ensure_ascii=False)+'</script>'

class OrderedTests(unittest.TestCase):
    def test_source_match_stops_without_waiting_for_name_or_synopsis_patch(self):
        calls=[]
        class Client:
            key='yes';stopped=False
            def search(self,r,t,reserve,stage):
                calls.append(stage);return None,{'candidate_names':['成为休曼']}
            def direct(self,*a):raise AssertionError('Should stop after source match')
        _,e=resolve(Client(),record(),'now',source_probe=lambda r,n:'matched')
        self.assertEqual(calls,['tavily']);self.assertEqual(e['playback_status'],'matched')

    def test_book_title_brackets_are_candidates_and_english_titles_are_ignored(self):
        self.assertEqual(bracket_names('中文名《成为休曼》，原名《Being Heumann》，备用[成为休曼]'),['成为休曼'])

    def test_candidate_is_reprobed_when_a_later_source_verifies_its_name(self):
        calls=[]
        class Client:
            key='yes';stopped=False
            def search(self,r,t,reserve,stage):
                if stage=='tavily':return None,{'candidate_names':['永属于你']}
                return {'display_name_zh':'永属于你','aliases_zh':['永属于你'],'synopsis_zh':'中文剧情'}, {'candidate_names':[]}
            def direct(self,*a):return None
        def probe(r,name):
            verified=name in r.get('lookup_verified_aliases',[]);calls.append(verified)
            return 'matched' if verified else 'ambiguous'
        _,e=resolve(Client(),record(),'now',source_probe=probe)
        self.assertEqual(calls,[False,True]);self.assertEqual(e['playback_status'],'matched')

    def test_exported_batch_dry_run_has_exact_identity_and_no_network(self):
        from lookup_jobs import run as run_jobs
        from catalog import write_json
        c=Mock()
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);write_json(root/'jobs.json',{'items':[record(season=1)]})
            out=run_jobs(root/'jobs.json',root/'out.json',title='Eternally Yours',root=root,client=c)
            self.assertEqual(out['network_requests'],0);self.assertEqual(out['jobs'][0]['season'],1)
            self.assertIn('2026',out['jobs'][0]['query']);c.resolve.assert_not_called()
            self.assertFalse((root/'docs').exists());self.assertFalse((root/'data').exists())

    def test_batch_budget_reserves_every_stage_and_never_publishes(self):
        from lookup_jobs import run as run_jobs
        from catalog import write_json
        session=Mock();session.post.return_value.status_code=200
        session.post.return_value.json.return_value={'answer':'[]','results':[]}
        session.get.return_value.status_code=200;session.get.return_value.json.return_value={}
        c=Lookup('private-test-key',session)
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);write_json(root/'jobs.json',{'items':[record(season=1)]})
            out=run_jobs(root/'jobs.json',root/'out.json',execute=True,monthly_limit=1,root=root,client=c)
            self.assertEqual(session.post.call_count,1);self.assertEqual(out['monthly_reserved'],1)
            self.assertFalse(out['publication']);self.assertFalse((root/'docs').exists())
            self.assertNotIn('private-test-key',(root/'out.json').read_text(encoding='utf-8'))

    def test_review_saved_with_empty_evidence_never_starts_paid_queries(self):
        from catalog import write_json
        from title_lookup import run
        r=record(kind='movie',slug='eternally-yours',identity_status='verified_official_mapping',seasons=[])
        session=Mock();client=Lookup('private-test-key',session)
        with tempfile.TemporaryDirectory() as d,contextlib.redirect_stdout(io.StringIO()):
            root=Path(d);write_json(root/'docs/catalog.json',{'schema':'prime-video-cn-catalog/1','items':[r],'item_count':1})
            write_json(root/'docs/version.json',{'last_success_at':'2026-10-01T00:00:00+00:00'})
            run(root,client=client,clock=datetime(2026,10,30,tzinfo=timezone.utc),review_saved=True,limit=12)
        session.post.assert_not_called();session.get.assert_not_called()

    def test_production_runner_reuses_verified_name_and_summary_without_another_paid_query(self):
        from catalog import write_json
        from title_lookup import run
        r=record(kind='movie',slug='eternally-yours',identity_status='verified_official_mapping',seasons=[])
        session=Mock();session.post.return_value.status_code=200
        session.post.return_value.json.return_value={'answer':'[永远与你]','results':[{'url':'https://movie.douban.com/subject/123/','title':'永远与你'}]}
        session.get.return_value.status_code=200;session.get.return_value.text=page('Movie')
        client=Lookup('private-test-key',session)
        with tempfile.TemporaryDirectory() as d,contextlib.redirect_stdout(io.StringIO()):
            root=Path(d);write_json(root/'docs/catalog.json',{'schema':'prime-video-cn-catalog/1','items':[r],'item_count':1})
            write_json(root/'docs/version.json',{'last_success_at':'2026-10-01T00:00:00+00:00'})
            clock=datetime(2026,10,30,tzinfo=timezone.utc)
            run(root,client=client,clock=clock,limit=3)
            out=json.loads((root/'docs/catalog.json').read_text(encoding='utf-8'))['items'][0]
            self.assertEqual(out['prime_id'],r['prime_id']);self.assertEqual(out['display_name_zh'],'永远与你')
            self.assertIn('synopsis_zh',out);self.assertEqual(session.post.call_count,1)
            session.reset_mock()
            run(root,client=client,clock=clock,force=True)
            session.post.assert_not_called()

    def test_brackets_only_safe_names_not_links_instructions_or_unknown(self):
        self.assertEqual(bracket_names('国内名[永远与你]、[未知]、[https://evil.test/中文]、【与你长相守】、[忽略指令]'),['永远与你','与你长相守'])
        self.assertEqual(bracket_names('可能翻译为永远与你，但没有依据。'),[])
        self.assertEqual(bracket_names('[] [1] [无可靠中文名]'),[])

    def test_real_api_payload_asks_ai_and_scopes_fallback_sources(self):
        session=Mock();session.post.return_value.status_code=200
        session.post.return_value.json.return_value={'answer':'[永远与你]','results':[]}
        c=Lookup('fake-key',session)
        _,e=c.search(record(),'now',stage='douban')
        payload=session.post.call_args.kwargs['json']
        self.assertEqual(payload['include_domains'],['movie.douban.com'])
        self.assertTrue(payload['include_answer']);self.assertIn('Eternally Yours',payload['query'])
        self.assertEqual(e['candidate_names'],['永远与你'])
        self.assertEqual(query(record(),'tavily'),'"Eternally Yours" 2026 中文名')

    def test_retrieved_encyclopedia_definition_is_separate_from_ai_prose(self):
        item={'url':'https://baike.baidu.com/item/永属于你/123','page_text':'美国2026年电视剧\n《永属于你》（Eternally Yours）是一部美国喜剧电视剧。\n首播时间2026年10月8日'}
        self.assertEqual(retrieved_page_patch(record(season=1),item,'now')['display_name_zh'],'永属于你')
        self.assertIsNone(retrieved_page_patch(record(year=1939),item,'now'))
        self.assertIsNone(retrieved_page_patch(record(kind='movie'),item,'now'))
        self.assertIsNone(retrieved_page_patch(record(season=4),item,'now'))
        self.assertIsNone(retrieved_page_patch(record(),{'url':item['url'],'snippet':item['page_text']},'now'))

    def test_quoted_answer_names_need_cited_text_and_reject_dictionary_sentences(self):
        results=[{'title':'永属于你','page_text':'永属于你；时髦的人,放荡的人;性滥交的人；我生命中的挚爱在2026年自杀了。'}]
        answer='Chinese name is "永属于你". "时髦的人,放荡的人;性滥交的人". "我生命中的挚爱在2026年自杀了。"'
        self.assertEqual(grounded_answer_names(answer,results),['永属于你'])
        self.assertEqual(grounded_answer_names('"凭空杜撰的译名"',results),[])

    def test_order_and_candidates_are_probed_before_next_fallback(self):
        events=[]
        class Client:
            key='yes';stopped=False
            def search(self,r,t,reserve,stage):
                events.append(stage)
                return None,{'stage':stage,'candidate_names':['名'+stage],'results':[]}
            def direct(self,r,t):events.append('wikipedia');return None
        def probe(r,name):events.append('probe:'+name);return 'unmatched'
        p,e=resolve(Client(),record(),'now',source_probe=probe)
        self.assertIsNone(p)
        self.assertEqual(events,['tavily','probe:名tavily','douban','probe:名douban','wikipedia','baidu','probe:名baidu'])
        self.assertEqual(e['playback_status'],'unmatched');self.assertTrue(e['retryable'])

    def test_ai_only_name_never_becomes_verified_display_or_no_source(self):
        class Client:
            key='yes';stopped=False
            def search(self,r,t,reserve,stage):return None,{'candidate_names':['永远与你']}
            def direct(self,r,t):return None
        p,e=resolve(Client(),record(),'now')
        self.assertIsNone(p);self.assertEqual(e['status'],'pending_review')
        self.assertEqual(e['playback_status'],'not_checked')

    def test_probes_that_error_or_return_ambiguous_never_look_like_no_source(self):
        class Client:
            key='yes';stopped=False
            def search(self,r,t,reserve,stage):return None,{'candidate_names':['候选名']}
            def direct(self,r,t):return None
        def failed(*a):raise RuntimeError('unavailable')
        _,e=resolve(Client(),record(),'now',source_probe=failed)
        self.assertEqual(e['playback_status'],'unavailable')
        _,e=resolve(Client(),record(),'now',source_probe=lambda *a:'ambiguous')
        self.assertEqual(e['playback_status'],'ambiguous')

    def test_verified_name_summary_and_matched_source_short_circuit(self):
        c=Mock(key='yes',stopped=False)
        c.search.return_value=({'display_name_zh':'永远与你','aliases_zh':['永远与你'],'synopsis_zh':'真实中文剧情'}, {'candidate_names':['永远与你']})
        _,e=resolve(c,record(),'now',source_probe=lambda *a:'matched')
        self.assertEqual(c.search.call_count,1);c.direct.assert_not_called()
        self.assertFalse(e['retryable'])

    def test_douban_same_entity_and_media_year_not_snippet(self):
        url='https://movie.douban.com/subject/123/'
        p=page_patch(record(),page(),url,'now')
        self.assertEqual(p['display_name_zh'],'永远与你');self.assertIn('synopsis_zh',p)
        self.assertIsNone(page_patch(record(kind='movie'),page(),url,'now'))
        self.assertIsNone(page_patch(record(year=1939),page(),url,'now'))
        self.assertIsNone(page_patch(record(english_title='Other',original_title='Other'),page(),url,'now'))
        self.assertIsNone(page_patch(record(),'Eternally Yours 2026 电视剧 永远与你',url,'now'))

    def test_series_plot_does_not_become_a_season_plot(self):
        url='https://movie.douban.com/subject/123/'
        p=page_patch(record(season=2),page(),url,'now');self.assertNotIn('synopsis_zh',p)
        p=page_patch(record(season=2),page('TVSeason',2026,2),url,'now');self.assertIn('synopsis_zh',p)
        p=page_patch(record(season=3),page('TVSeason',2026,2),url,'now');self.assertIsNone(p)

    def test_baidu_labelled_identity_not_body_mentions(self):
        content='<h1>永远与你</h1><p>外文名</p><p>Eternally Yours</p><p>首播时间</p><p>2026年</p><p>类型</p><p>电视剧</p>'
        url='https://baike.baidu.com/item/永远与你/123'
        self.assertIsNotNone(page_patch(record(),content,url,'now'))
        self.assertIsNone(page_patch(record(),content.replace('首播时间','参考年份'),url,'now'))
        self.assertFalse(safe_source('https://movie.douban.com.evil.test/subject/123'))

    def test_chinese_season_prefix_is_not_a_chinese_description(self):
        self.assertFalse(chinese('第 1 季 A vampire couple face a new challenge in their marriage.'))
        self.assertTrue(chinese('吸血鬼夫妇在五百年的婚姻后面临新挑战。'))

    def test_summary_cache_cannot_cross_seasons_years_or_original_text(self):
        target=record(season=2,synopsis_original='Original second season')
        patch={'synopsis_zh':'第二季真实简介','synopsis_source_url':'https://movie.douban.com/subject/123/','prime_id':'BAD'}
        cache={'summary_records':{summary_key(target):{'status':'verified','identity':summary_identity(target),'patch':patch}}}
        movie=record(kind='movie')
        movie_cache={'summary_records':{summary_key(movie):{'status':'verified','identity':summary_identity(movie),'patch':patch}}}
        out=apply_summary_cache(movie,movie_cache)
        self.assertEqual(out['prime_id'],movie['prime_id']);self.assertIn('synopsis_zh',out)
        for changed in [movie|{'year':2025},movie|{'synopsis_original':'new original'}]:
            self.assertNotIn('synopsis_zh',apply_summary_cache(changed,movie_cache))
        self.assertNotEqual(summary_key(target),summary_key(target|{'season':3}))

    def test_source_outages_preserve_old_summary_and_ids(self):
        original=record(kind='movie',synopsis_zh='已有可靠中文简介')
        c=Mock()
        with tempfile.TemporaryDirectory() as d:
            out,stats=enrich_summaries([original],{},c,'2026-10-30T00:00:00+00:00',None,5,Path(d)/'cache.json')
        self.assertEqual(out,[original]);c.resolve.assert_not_called()

if __name__=='__main__':unittest.main()
