import unittest,sys,copy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from lookup_context import contextualize,resolved_aliases
from metadata_resolution import query,candidate_links,resolve
from unittest.mock import Mock

class ContextTests(unittest.TestCase):
 def setUp(self):
  self.target={'officialId':'B0BXL76TNN','originalTitle':'Batman','kind':'tv','year':1992,'season':1,'displayTitle':'Old','aliases':[]}
  self.context=self.target|{'displayTitle':'蝙蝠侠：动画版 第一季','lookupTitle':'Batman: The Animated Series','officialUrl':'https://www.primevideo.com/detail/0IY0UQ0BH91GDW55AZPO3U020P','candidateLinks':['https://movie.douban.com/subject/2272384/'],'reviewedAliases':['蝙蝠侠动画版']}
 def test_context_bound_to_all_identity_fields(self):
  result=contextualize([self.target],{'items':[self.context]})[0]
  self.assertEqual(result['displayTitle'],'蝙蝠侠：动画版 第一季');self.assertEqual(self.target['displayTitle'],'Old')
  for key,value in [('officialId','B012345678'),('kind','movie'),('year',1989),('season',2),('originalTitle','The Batman')]:
   wrong=self.context|{key:value};self.assertEqual(contextualize([self.target],{'items':[wrong]})[0],self.target)
 def test_ai_request_contains_official_link_full_title_year_and_season(self):
  r={'english_title':'Batman','lookup_title':self.context['lookupTitle'],'official_url':self.context['officialUrl'],'year':1992,'season':1,'kind':'tv'}
  q=query(r,'tavily')
  for s in [self.context['officialUrl'],'Batman: The Animated Series','1992','第1季','豆瓣']:self.assertIn(s,q)
  r['official_url']='https://private.example/token';self.assertNotIn('private.example',query(r,'tavily'))
 def test_ai_subject_links_are_candidates_only(self):
  self.assertEqual(candidate_links('https://movie.douban.com/subject/2272384 https://evil.example/subject/7/'),['https://movie.douban.com/subject/2272384/'])
 def test_provided_page_is_verified_before_search_and_blocking_is_pending(self):
  client=Mock();client.key='';client.stopped=False;client.verify_results.return_value=None;client.direct.return_value=None
  _,e=resolve(client,{'candidate_links':self.context['candidateLinks']},'now')
  self.assertEqual(e['stages'][0]['stage'],'provided_links');self.assertEqual(e['stages'][0]['status'],'pending_review');self.assertEqual(e['status'],'pending_review')
 def test_validated_candidate_can_stop_at_source_match(self):
  client=Mock();client.key='';client.stopped=False;client.verify_results.return_value={'display_name_zh':'蝙蝠侠：动画版','aliases_zh':['蝙蝠侠：动画版']}
  _,e=resolve(client,{'candidate_links':self.context['candidateLinks']},'now',source_probe=lambda *_:'matched')
  self.assertEqual(len(e['stages']),1);self.assertEqual(e['playback_status'],'matched')
 def test_only_verified_same_identity_aliases_feed_source_checks(self):
  from datetime import datetime,timezone
  now=datetime.now(timezone.utc)
  row={'identity':{'prime_id':self.target['officialId'],'kind':'tv','first_air_year':1992,'english_title':'Batman'},'season':1,'checked_at':now.isoformat(),
       'patch':{'display_name_zh':'蝙蝠侠：动画版','aliases_zh':['蝙蝠侠动画版'],'name_status':'verified_title_sources','sources':[{'url':self.context['candidateLinks'][0]}]}}
  cache={'batch_records':{'x':row}}
  result=resolved_aliases([self.target],cache,now)[0];self.assertIn('蝙蝠侠动画版',result['aliases'])
  for field,value in [('season',2),('checked_at','bad')]:
   bad=copy.deepcopy(row);bad[field]=value;self.assertEqual(resolved_aliases([self.target],{'batch_records':{'x':bad}},now)[0],self.target)
  bad=copy.deepcopy(row);bad['patch']['name_status']='ai_candidate';self.assertEqual(resolved_aliases([self.target],{'batch_records':{'x':bad}},now)[0],self.target)
