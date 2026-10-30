import sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from catalog import publish
from prune_publication import prune

class Retention(unittest.TestCase):
    def test_active_previous_and_referenced_data_are_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            docs=Path(temp);(docs/'images').mkdir()
            item={'id':'seed:1','slug':'one','kind':'tv','identity_status':'pending_review','display_name_zh':'作品','seasons':[]}
            versions=[];previous={'last_success_at':'2026-01-01T00:00:00+00:00'}
            for n in range(3):
                item['synopsis_zh']='简介'+str(n);previous=publish(docs,[item],previous);versions.append(previous)
            orphan=docs/'images'/('a'*64+'.jpg');orphan.write_bytes(b'not referenced')
            marker=docs/'images/.gitkeep';marker.write_bytes(b'')
            removed=prune(docs)
            self.assertIn(versions[0]['catalog_path'],removed)
            self.assertTrue((docs/versions[1]['catalog_path']).is_file())
            self.assertTrue((docs/versions[2]['catalog_path']).is_file())
            self.assertFalse(orphan.exists());self.assertTrue(marker.exists())
            self.assertEqual(prune(docs),[])

if __name__=='__main__':unittest.main()
