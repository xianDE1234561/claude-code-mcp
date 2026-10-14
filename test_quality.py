import unittest
import quality_eval as quality


class QualityTests(unittest.TestCase):
    def test_manifest_covers_defects_correct_code_and_advisory_traps(self):
        cases=quality.load_cases(); self.assertEqual(len(cases),4)
        self.assertEqual(sum(c['defect'] for c in cases),2)
        self.assertIn('sparse_docs_are_advisory',{c['id'] for c in cases})

    def test_failed_model_cases_are_not_silently_counted_as_correct(self):
        value=quality.metrics([
            {'defect':True,'model_report_complete':True,'matched_candidates':1,'candidate_count':1,'rejected_count':0,'elapsed_seconds':10},
            {'defect':False,'model_report_complete':True,'matched_candidates':0,'candidate_count':1,'rejected_count':1,'elapsed_seconds':20},
            {'defect':True,'model_report_complete':False,'matched_candidates':0,'candidate_count':0,'rejected_count':0,'elapsed_seconds':30}])
        self.assertEqual(value['completed_cases'],2); self.assertEqual(value['failed_or_partial_cases'],1)
        self.assertEqual(value['true_positive_cases'],1); self.assertEqual(value['false_positive_cases'],1)
        self.assertEqual(value['false_negative_cases'],0); self.assertEqual(value['evidence_validity_rate'],2/3)


if __name__=='__main__': unittest.main()
