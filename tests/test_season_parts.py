import sys,json,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from season_parts import season_fields
from sources import parse_prime,Clients,SourceError
from app_metadata import normalize_fields

class SeasonParts(unittest.TestCase):
 def test_explicit_evidence_and_no_numeric_guess(self):
  self.assertEqual(season_fields(102,'Example Season 1, Part 2'),{'season':1,'officialSeasonNumber':102,'seasonPart':2})
  self.assertEqual(season_fields(102,'Example'),{'season':102})
  self.assertEqual(season_fields(202,'Example Season 2, Part 2')['season'],2)
  with self.assertRaises(ValueError):season_fields(202,'Example Season 1, Part 2')
 def test_raw_identity_kept_and_expected_season_is_logical(self):
  ident='B0D6F6J4GK';data={'title':'ANGRY BIRDS: MYSTERY ISLAND – Season 1, Part 2','entityType':'TV Show','seasonNumber':102}
  state={'init':{'preparations':{'body':{'atf':{'state':{'pageTitleId':ident,'detail':{'headerDetail':{ident:data}}}}}}}}
  html='<script id="dv-web-page-hydration-data">'+json.dumps(state)+'</script>'
  parsed=parse_prime(html,ident,1);self.assertEqual(parsed['seasonNumber'],102);self.assertEqual(parsed['_logical_season'],1)
  with self.assertRaises(SourceError):parse_prime(html,ident,102)
 def test_part_metadata_cannot_overwrite_complete_season(self):
  import tempfile
  with tempfile.TemporaryDirectory() as d:
   client=Clients(Path(d),lambda *_:None)
   patch=client.prime_patch('B0D6F6J4GK',{'title':'Example Season 1, Part 2','entityType':'TV Show','seasonNumber':102})
  self.assertNotIn('seasons',patch);part=patch['official_unmapped_seasons'][0]
  self.assertEqual((part['season'],part['display_season'],part['part']),(102,1,2))
 def test_old_encoded_season_is_quarantined_without_merging_parts(self):
  item={'kind':'tv','seasons':[{'season':1,'official_id':'PARTONE123'},{'season':102,'official_title_zh':'Example Season 1, Part 2','official_id':'PARTTWO123'}]}
  result=normalize_fields([item])[0]
  self.assertEqual(len(result['seasons']),1);self.assertEqual(result['seasons'][0]['official_id'],'PARTONE123')
  self.assertEqual(result['official_unmapped_seasons'][0]['display_season'],1)
