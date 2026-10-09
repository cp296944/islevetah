import unittest
from datetime import datetime, timedelta
from server import estimate, TZ

class EstimateTests(unittest.TestCase):
    def rows(self, values, days):
        start=datetime(2026,5,1,tzinfo=TZ)
        return [dict(quantity=v,counted_at=(start+timedelta(days=d)).isoformat()) for v,d in zip(values,days)]
    def test_restock_excluded_with_time(self):
        e=estimate(self.rows([50,30,80,20],[0,5,8,18]),datetime(2026,5,19,tzinfo=TZ))
        self.assertAlmostEqual(e['daily'],80/15)
        self.assertEqual(e['valid_intervals'],2)
        self.assertEqual(e['excluded_intervals'],1)
        self.assertEqual(e['status'],'warning')
    def test_equal_stock_counts_as_time(self):
        self.assertEqual(estimate(self.rows([50,50,30],[0,5,10]))['daily'],2)
    def test_single_record_insufficient(self):
        self.assertIsNone(estimate(self.rows([50],[0]))['daily'])
    def test_zero_stock(self):
        self.assertEqual(estimate(self.rows([0],[0]))['status'],'empty')
    def test_zero_consumption(self):
        e=estimate(self.rows([20,20],[0,7]))
        self.assertEqual(e['status'],'stable')
        self.assertIsNone(e['depletion'])
    def test_actual_six_days(self):
        self.assertEqual(estimate(self.rows([50,20],[0,6]))['weekly'],35)
    def test_warning_boundary(self):
        rows=self.rows([60,50],[0,1])
        at=datetime(2026,5,2,tzinfo=TZ)
        self.assertEqual(estimate(rows,at)['status'],'normal')
        self.assertEqual(estimate(rows,at+timedelta(seconds=1))['status'],'warning')
    def test_recent_six_intervals(self):
        e=estimate(self.rows([100,50,49,48,47,46,45,44],list(range(8))))
        self.assertEqual(e['daily'],1)
    def test_reversed_input(self):
        self.assertEqual(estimate(list(reversed(self.rows([50,20],[0,7]))))['weekly'],30)

if __name__=='__main__': unittest.main()
