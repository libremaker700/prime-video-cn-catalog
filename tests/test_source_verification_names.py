import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from check_source_verification import calibrated_targets


class NameIntegrationTests(unittest.TestCase):
    def test_only_same_identity_verified_names_join_source_queries(self):
        target = {'officialId':'B012345678','originalTitle':'Example Film','kind':'movie','season':0,'year':2024,'displayTitle':'Example Film','aliases':[]}
        item = {'prime_id':'B012345678','english_title':'Example Film','kind':'movie','name_status':'verified_title_sources','display_name_zh':'核实片名','aliases_zh':['另一译名']}
        result = calibrated_targets([target], {'items':[item]})[0]
        self.assertEqual(result['displayTitle'],'核实片名'); self.assertEqual(result['year'],2024)
        self.assertEqual(target['displayTitle'],'Example Film')
        for patch in ({'prime_id':'B000000000'},{'english_title':'Another film'},{'name_status':'pending_review'},{'kind':'tv'}):
            self.assertEqual(calibrated_targets([target],{'items':[item|patch]})[0],target)


if __name__ == '__main__': unittest.main()
