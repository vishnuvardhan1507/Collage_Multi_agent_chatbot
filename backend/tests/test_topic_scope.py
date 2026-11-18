"""Topic classification is interpreted by the model, enforced before any tools."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from flask import Flask
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from agents.guardrail_agent import guardrail_node
from agents.graph import build_graph


class TopicScopeTests(unittest.TestCase):
    def setUp(self):
        context=Flask(__name__).app_context()
        context.push()
        self.addCleanup(context.pop)

    def state(self, query):
        return {'user_id':'STU001','role':'student','query':query,'chat_history':[], 'agent_trace':[]}

    def test_unrelated_questions_stop_before_supervisor_and_tools(self):
        for query,reply in [('who is mukesh ambani','I focus on college services, so I cannot provide that biography.'),('Write a movie review','Movie reviews are outside my role. Can I help with your college services?')]:
            with self.subTest(query=query), patch('agents.guardrail_agent.fetch_one',return_value={'role':'student'}), patch('agents.guardrail_agent.llm.invoke_json',return_value={'verdict':'allow','topic_scope':'unrelated','reason':reply,'modules':['general']}) as model:
                result=build_graph().invoke(self.state(query))
                self.assertEqual(result['final_response'],reply)
                self.assertEqual(result['agent_trace'],['supervisor_intake','guardrail'])
                self.assertEqual(model.call_count,1)

    def test_missing_generated_refusal_reports_failure(self):
        for reason in [None,'',[],{}]:
            with patch('agents.guardrail_agent.fetch_one',return_value={'role':'student'}),patch('agents.guardrail_agent.llm.invoke_json',return_value={'verdict':'deny','topic_scope':'unrelated','reason':reason}):
                with self.assertRaises(ValueError): guardrail_node(self.state('unrelated question'))

    def test_malformed_classification_cannot_allow(self):
        for scope in [None,'unknown','',42,[],{}]:
            with patch('agents.guardrail_agent.fetch_one',return_value={'role':'student'}),patch('agents.guardrail_agent.llm.invoke_json',return_value={'verdict':'allow','topic_scope':scope}):
                self.assertEqual(guardrail_node(self.state('question'))['guardrail_verdict'],'deny')

    def test_greetings_and_college_followups_still_allowed(self):
        for query,scope in [('Hello','social'),('What can you do?','social'),('What is my attendance?','college'),('delete both','college'),('Make it 70%','college'),('And tomorrow?','college')]:
            with patch('agents.guardrail_agent.fetch_one',return_value={'role':'student'}),patch('agents.guardrail_agent.llm.invoke_json',return_value={'verdict':'allow','topic_scope':scope,'modules':['attendance']}) as model:
                state=self.state(query);state['chat_history']=[{'role':'user','content':'Show my alerts'}]
                result=guardrail_node(state)
                self.assertEqual(result['guardrail_verdict'],'allow')
                self.assertEqual(model.call_args.args[1]['chat_history'],state['chat_history'])


if __name__=='__main__': unittest.main()
