from datetime import datetime,timezone
import unittest
from services.analysis_api.operations import account_interval,observe,action,DAILY_SECONDS,MONTHLY_SECONDS


class OperationTests(unittest.TestCase):
    def test_startup_idle_and_stopping_are_counted_across_midnight(self):
        start=datetime(2026,9,29,23,59,0,tzinfo=timezone.utc).timestamp()
        result=account_interval({},start,start+120)
        self.assertEqual(result['daily_seconds']['2026-09-29'],60)
        self.assertEqual(result['daily_seconds']['2026-09-30'],60)
        first=observe(None,'running',start+120,start)
        second=observe(first,'stopped',start+180,start)
        self.assertEqual(second['monthly_seconds']['2026-09'],180)
        third=observe(second,'stopped',start+240,start)
        self.assertEqual(third['monthly_seconds']['2026-09'],180)

    def test_stopping_race_waits_then_starts_once_stopped(self):
        budget={'compute_allowed':True}
        self.assertEqual(action('stopping',True,budget),'wait_for_stopped_event')
        self.assertEqual(action('stopped',True,budget),'start')
        self.assertEqual(action('pending',True,budget),'none')

    def test_daily_monthly_and_spend_caps_stop_new_work(self):
        now=datetime(2026,9,29,tzinfo=timezone.utc).timestamp()
        for change in ({'daily_seconds':{'2026-09-29':DAILY_SECONDS}},
                       {'monthly_seconds':{'2026-09':MONTHLY_SECONDS}},
                       {'monthly_compute_usd':{'2026-09':40}}):
            result=observe({'last_accounted_at':now,'observed_state':'running',**change},'running',now,now)
            self.assertFalse(result['compute_allowed'])
            self.assertEqual(action('running',True,result),'stop')
        self.assertFalse(result['uploads_allowed'])
