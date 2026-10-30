import copy, json, sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from app_metadata import apply_app_metadata,compact_id,normalize_fields

class AppMappings(unittest.TestCase):
    def fixture(self):
        item={'id':'prime:B012345678','prime_id':'B012345678','kind':'tv','identity_status':'verified_official_mapping','original_title':None,'seasons':[{'season':2,'official_id':None,'synopsis_zh':'原简介'}]}
        asset={'schema':'prime-video-app-metadata/1','items':[{'prime_id':'B012345678','kind':'tv','english_title':'Example','seasons':[{'season':2,'official_link':'/-/zh/detail/0R4L986D9M1W0RF5Y2I4CPFZ6K?ref_=s2'}]}]}
        return item,asset
    def test_missing_season_and_original_identity_import_preserves_content(self):
        item,asset=self.fixture();result=apply_app_metadata([item],asset)[0]
        self.assertEqual(result['english_title'],'Example');self.assertEqual(result['original_title'],'Example')
        self.assertEqual(result['seasons'][0]['official_compact_id'],'0R4L986D9M1W0RF5Y2I4CPFZ6K')
        self.assertEqual(result['seasons'][0]['synopsis_zh'],'原简介');self.assertIsNone(result['seasons'][0]['official_id'])
        self.assertIsNone(item['original_title'])
    def test_unknown_media_and_conflicting_official_ids_do_not_silently_merge(self):
        item,asset=self.fixture();item['identity_status']='pending_review'
        self.assertNotIn('english_title',apply_app_metadata([item],asset)[0])
        item['identity_status']='verified_official_mapping';asset['items'][0]['kind']='movie'
        with self.assertRaises(ValueError):apply_app_metadata([item],asset)
        asset['items'][0]['kind']='tv';item['seasons'][0]['official_id']='B000000001';asset['items'][0]['seasons'][0]['official_id']='B000000002'
        with self.assertRaises(ValueError):apply_app_metadata([item],asset)
    def test_link_validation_and_nested_dates(self):
        self.assertIsNone(compact_id('https://evil.example/-/zh/detail/B012345678'))
        self.assertIsNone(compact_id('/-/zh/detail/../../private'))
        movie={'kind':'movie','details':{'release_date':'2020-01-01','genres':'剧情'},'release_date':'2021-01-01'}
        result=normalize_fields([movie])[0]
        self.assertEqual(result['release_date'],'2021-01-01');self.assertEqual(result['genres'],'剧情')

    def test_official_animation_movie_is_not_treated_as_a_tv_anime(self):
        movie={'kind':'anime','identity_status':'verified_official_mapping','details':{'official_type':'Movie','release_date':'2015-01-01'}}
        result=normalize_fields([movie])[0]
        self.assertEqual(result['kind'],'movie');self.assertEqual(result['release_date'],'2015-01-01')

    def test_encoded_specials_stay_outside_supported_seasons_and_image_manifest(self):
        item={'kind':'anime','seasons':[{'season':4,'synopsis_zh':'第四季'},{'season':202,'official_title_zh':'特别篇','official_compact_id':'0R4L986D9M1W0RF5Y2I4CPFZ6K','synopsis_zh':'特别篇简介','poster':{'path':'images/special.jpg'},'poster_scope':'season'}]}
        result=normalize_fields([item])[0]
        self.assertEqual(result['seasons'],[item['seasons'][0]])
        extra=result['official_unmapped_seasons'][0]
        self.assertEqual((extra['season'],extra['synopsis_zh']),(202,'特别篇简介'))
        self.assertNotIn('poster',extra);self.assertNotIn('poster_scope',extra)
        self.assertEqual(normalize_fields([result]),[result])

if __name__=='__main__':unittest.main()
