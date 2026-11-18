"""Multiple menu dishes, legacy compatibility and grounded chat operations."""
import json
import unittest
from contextlib import closing
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo
import test_faculty_alerts as fixtures
from agents import llm
from agents.prompts import GUARDRAIL_AGENT_PROMPT
from tools import alerts
from tools.db_tool import get_connection, fetch_all
from tools.notifications import deliver_one
from tools.operations import OperationError


class MultiDishTests(unittest.TestCase):
    setUpClass=classmethod(fixtures.FacultyAlertTests.setUpClass.__func__)
    tearDownClass=classmethod(fixtures.FacultyAlertTests.tearDownClass.__func__)
    setUp=fixtures.FacultyAlertTests.setUp
    write=fixtures.FacultyAlertTests.write
    headers=fixtures.FacultyAlertTests.headers

    def now(self,hour=7,day=26):
        return datetime(2026,10,day,hour,tzinfo=ZoneInfo('Asia/Kolkata'))

    def create(self,dishes=None,**extra):
        config={'plans':['non_veg'],'meals':['lunch','dinner'],'dishes':dishes or ['chicken','egg']}
        config.update(extra)
        with patch.object(alerts,'college_now',return_value=self.now(6)):
            return alerts.create_alert('STU001','student','menu_match',config)

    def evaluate(self,now=None):
        with closing(get_connection()) as conn,conn:
            conn.execute('BEGIN IMMEDIATE');alerts.evaluate_due(conn,now or self.now())

    def test_any_matching_dishes_meals_and_plans(self):
        self.create()
        for lunch,dinner,expected in [('Chicken curry','Rice',1),('Rice','Boiled EGG',1),('Chicken curry','Egg masala',1),('Rice','Dal',0),('Eggplant','Rice',0)]:
            self.write('DELETE FROM alert_occurrences');self.write('DELETE FROM notification_outbox')
            self.write("UPDATE mess_menus SET lunch=?,dinner=? WHERE weekday=0 AND plan_id='non_veg'",(lunch,dinner))
            self.evaluate();self.evaluate()
            rows=fetch_all('SELECT * FROM alert_occurrences')
            self.assertEqual(len(rows),expected)
            if expected:
                self.assertIn('non veg',rows[0]['body'])
                if 'Chicken' in lunch:self.assertIn('lunch (chicken)',rows[0]['body'])
                if 'egg' in dinner.lower():self.assertIn('dinner (egg)',rows[0]['body'])
        self.write("UPDATE mess_menus SET lunch='Chicken',dinner='Egg' WHERE weekday=0 AND plan_id='veg'")
        self.evaluate();self.assertEqual(fetch_all('SELECT * FROM alert_occurrences'),[])

    def test_all_requires_every_dish_in_the_same_plan_and_meal(self):
        self.create(match_mode='all')
        cases = [
            ('Chicken curry with egg masala','Rice',1),
            ('Chicken curry','Egg masala',0),
            ('Chicken curry with eggplant','Rice',0),
            (None,'Chicken curry with egg masala',1),
            (None,None,0),
        ]
        for lunch,dinner,expected in cases:
            self.write('DELETE FROM alert_occurrences');self.write('DELETE FROM notification_outbox')
            self.write("UPDATE mess_menus SET lunch=?,dinner=? WHERE weekday=0 AND plan_id='non_veg'",(lunch,dinner))
            self.evaluate();self.evaluate()
            rows=fetch_all('SELECT * FROM alert_occurrences')
            self.assertEqual(len(rows),expected)
            if expected:
                self.assertIn('chicken, egg',rows[0]['body'].lower())
        # A dish in each of two plans is not a same-row ALL match.
        self.write('DELETE FROM alert_occurrences');self.write('DELETE FROM notification_outbox')
        self.write("UPDATE alert_rules SET config_json=?",(json.dumps({'plans':['veg','non_veg'],'meals':['lunch'],'dishes':['chicken','egg'],'match_mode':'all'}),))
        self.write("UPDATE mess_menus SET lunch='Chicken curry' WHERE weekday=0 AND plan_id='non_veg'")
        self.write("UPDATE mess_menus SET lunch='Egg curry' WHERE weekday=0 AND plan_id='veg'")
        self.evaluate();self.assertEqual(fetch_all('SELECT * FROM alert_occurrences'),[])

    def test_legacy_normalization_validation_and_literal_matching(self):
        rule=self.create([' Chicken ','chicken','EGG'])
        self.assertEqual(rule['config']['dishes'],['Chicken','EGG'])
        with patch.object(alerts,'college_now',return_value=self.now(6)):
            legacy=alerts.create_alert('FAC001','faculty','menu_match',{'plans':['non_veg'],'meals':['lunch'],'dish':'chicken'})
        self.assertIn('dish',legacy['config'])
        self.assertEqual(legacy['config']['match_mode'],'any')
        self.evaluate();self.assertEqual(len(fetch_all('SELECT * FROM alert_occurrences')),2)
        for dishes in [[], 'egg', [None], [True], [{}], [' '], ['x'*81], ['x']*11, ['egg\nchicken']]:
            with self.assertRaises(OperationError):
                alerts.create_alert('STU001','student','menu_match',{'plans':['veg'],'meals':['lunch'],'dishes':dishes})
        for extra in [{'dish':'chicken'}, {'match':'all'}, {'match_mode':'both'}, {'match_mode':True}, {'plans':[{}]}, {'meals':['lunch; DROP TABLE users']}]:
            with self.assertRaises(OperationError):self.create(**extra)
        self.create(["' OR 1=1 --",'.*'])
        self.evaluate();self.assertEqual(len(fetch_all('SELECT * FROM alert_occurrences')),2)

    def test_scheduling_edit_dedup_missing_and_retries(self):
        rule=self.create()
        self.evaluate(self.now(6));self.assertEqual(fetch_all('SELECT * FROM alert_occurrences'),[])
        self.evaluate()
        with patch.object(alerts,'college_now',return_value=self.now()):
            alerts.update_alert('STU001','student',rule['id'],'menu_match',{'plans':['non_veg'],'meals':['lunch','dinner'],'dishes':['chicken','egg','paneer']})
        self.assertEqual(len(fetch_all('SELECT * FROM alert_occurrences')),1)
        with patch.object(alerts,'college_now',return_value=self.now()),patch('auth.mail.send_email',side_effect=OSError('mock')):
            self.assertTrue(deliver_one())
        self.write('UPDATE notification_outbox SET next_attempt=0')
        with patch.object(alerts,'college_now',return_value=self.now()),patch('auth.mail.send_email') as mail:
            self.assertTrue(deliver_one());self.assertEqual(mail.call_count,1)
        self.evaluate(self.now(day=27));self.assertEqual(len(fetch_all('SELECT * FROM alert_occurrences')),2)
        self.write("UPDATE mess_menus SET lunch=NULL,dinner=NULL WHERE weekday=2")
        self.evaluate(self.now(day=28));self.assertEqual(len(fetch_all('SELECT * FROM alert_occurrences')),2)
        self.assertEqual(self.client.delete('/api/alerts/'+str(rule['id']),headers=self.headers('FAC001')).status_code,404)
        alerts.delete_alert('STU001','student',rule['id'])
        self.assertEqual(fetch_all('SELECT * FROM alert_occurrences'),[])
        states=[r['status'] for r in fetch_all('SELECT status FROM notification_outbox')]
        self.assertEqual(sorted(states),['sent','suppressed'])

    def test_chat_any_mode_followups(self):
        requests=[('Notify me when chicken or egg is served',['chicken','egg']),
                  ('add paneer too',['chicken','egg','paneer']),('remove egg',['chicken','paneer'])]
        for user in ('STU001','FAC001'):
            for query,dishes in requests:
                def model(prompt,payload):
                    if prompt==GUARDRAIL_AGENT_PROMPT:return {'verdict':'allow','topic_scope':'college','modules':['mess']}
                    self.assertIn('match_mode=any',prompt)
                    rules=payload['alert_context']['owned_alerts']
                    result={'action':'update' if rules else 'create','kind':'menu_match','config':{'plans':['veg','non_veg'],'meals':['lunch','dinner'],'dishes':dishes,'match_mode':'any'}}
                    if rules:result['id']=rules[0]['id'];self.assertTrue(payload['chat_history'])
                    return {'route':'direct','alert_update':result}
                with patch.object(llm,'invoke_json',side_effect=model),patch.object(llm,'invoke_text',side_effect=AssertionError('No generated SQL')):
                    response=self.client.post('/api/chat',headers=self.headers(user),json={'session_id':'multi-dish','message':query})
                self.assertEqual(response.status_code,200,response.json)
                self.assertEqual(response.json['actions'],['alert_saved'])
            role='student' if user.startswith('STU') else 'faculty'
            self.assertEqual(alerts.list_alerts(user,role)['alerts'][0]['config']['dishes'],['chicken','paneer'])

    def test_chat_switches_between_all_and_any_and_clarifies_ambiguous_lists(self):
        messages=[('Notify me when chicken and egg are in the same meal','create','all'),
                  ('Change it to OR','update','any'),('Require both again','update','all')]
        def model(prompt,payload):
            if prompt==GUARDRAIL_AGENT_PROMPT:return {'verdict':'allow','topic_scope':'college','modules':['mess']}
            self.assertIn('same recorded plan-and-meal entry',prompt)
            self.assertIn('ask whether ANY or ALL',prompt)
            if payload['query']=='Notify me when chicken, egg is served':
                return {'route':'direct','direct_answer':'Should this alert match any selected dish or require all dishes in the same meal?'}
            action,mode=next((action,mode) for query,action,mode in messages if query==payload['query'])
            rules=payload['alert_context']['owned_alerts']
            update={'action':action,'kind':'menu_match','config':{'plans':['non_veg'],'meals':['lunch','dinner'],'dishes':['chicken','egg'],'match_mode':mode}}
            if action=='update':update['id']=rules[0]['id']
            return {'route':'direct','alert_update':update}
        with patch.object(llm,'invoke_json',side_effect=model),patch.object(llm,'invoke_text',side_effect=AssertionError('No generated SQL')):
            for message,_,mode in messages:
                response=self.client.post('/api/chat',headers=self.headers('STU001'),json={'session_id':'operators','message':message})
                self.assertEqual(response.status_code,200,response.json)
                self.assertEqual(response.json['actions'],['alert_saved'])
                rule=alerts.list_alerts('STU001','student')['alerts'][0]
                self.assertEqual(rule['config']['match_mode'],mode)
                self.assertIn(' and ' if mode=='all' else ' or ',rule['summary'])
            response=self.client.post('/api/chat',headers=self.headers('STU001'),json={'session_id':'operators','message':'Notify me when chicken, egg is served'})
        self.assertEqual(response.json['actions'],[])
        self.assertIn('any selected dish or require all',response.json['response'])
        self.assertEqual(len(alerts.list_alerts('STU001','student')['alerts']),1)


if __name__=='__main__':unittest.main()
