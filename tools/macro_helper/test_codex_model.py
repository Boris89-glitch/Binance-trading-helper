"""Subscription adapter tests; no live model calls."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from . import analysis,codex_model
from .test_analysis import SUMMARY,SOURCES


class CodexChecks(unittest.TestCase):
    def test_codex_connection_uses_subscription_without_key(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(analysis,'LOCAL_DIR',Path(folder)),patch.object(codex_model,'status',return_value={'configured':True,'detail':'Signed in with ChatGPT'}):
            for saved in ({'provider':'codex'}, {'provider':'codex','model':'gpt-6.1-sol'}):
                Path(folder,'settings.json').write_text(json.dumps(saved),encoding='utf-8')
                status=analysis.settings_status()
                self.assertEqual(status['provider'],'codex')
                self.assertEqual(status['model'],'gpt-6.1-sol')
                self.assertTrue(status['configured'])
            self.assertEqual(analysis.save_settings({'provider':'codex'})['provider'],'codex')

    def test_subscription_environment_excludes_api_credentials(self):
        with patch.dict('os.environ',{'OPENAI_API_KEY':'test','CODEX_API_KEY':'test','ANTHROPIC_API_KEY':'test','CODEX_THREAD_ID':'parent'}):
            env=codex_model.cli_environment()
        for key in ('OPENAI_API_KEY','CODEX_API_KEY','ANTHROPIC_API_KEY','CODEX_THREAD_ID'):
            self.assertNotIn(key,env)

    def test_api_key_login_is_not_treated_as_subscription(self):
        with patch.object(codex_model,'executable',return_value='codex'),patch.object(codex_model,'run',return_value=subprocess.CompletedProcess([],0,'Logged in using an API key','')):
            self.assertFalse(codex_model.status()['configured'])

    def test_inference_uses_isolated_structured_output_and_retains_plan_usage(self):
        def run(args,**kwargs):
            self.assertNotIn('PRIVATE EVIDENCE',str(args))
            self.assertIn('PRIVATE EVIDENCE',kwargs['input'])
            self.assertIn('--ignore-user-config',args);self.assertIn('read-only',args)
            Path(args[args.index('--output-last-message')+1]).write_text(json.dumps(SUMMARY),encoding='utf-8')
            return subprocess.CompletedProcess(args,0,json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':20}}),'')
        with patch.object(codex_model,'status',return_value={'configured':True}),patch.object(codex_model,'executable',return_value='codex'),patch.object(codex_model,'run',side_effect=run):
            result,usage,model=codex_model.infer({'evidence':'PRIVATE EVIDENCE','source_ledger':SOURCES},{'model':'gpt-6-luna'})
        self.assertEqual(result,SUMMARY);self.assertEqual(usage['billing_mode'],'chatgpt_subscription')
        self.assertIsNone(analysis.usage_cost(usage,model))

    def test_switch_does_not_require_key_or_expose_previous_key(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(analysis,'LOCAL_DIR',Path(folder)),patch.object(codex_model,'status',return_value={'configured':True,'detail':'Signed in with ChatGPT'}):
            analysis.save_settings({'provider':'anthropic','api_key':'sk-ant-existing-test'})
            status=analysis.save_settings({'provider':'codex'})
            self.assertEqual(status['model'],'gpt-6.1-sol');self.assertTrue(status['configured'])
            self.assertNotIn('sk-ant',json.dumps(status))
            self.assertEqual(analysis.settings()['provider'],'codex')


if __name__=='__main__':unittest.main()
