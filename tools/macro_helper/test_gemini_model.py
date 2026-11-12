"""Gemini free-tier adapter tests; no live model calls."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from . import analysis,gemini_model
from .test_analysis import SUMMARY,SOURCES


class GeminiChecks(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory();self.local=patch.object(analysis,'LOCAL_DIR',Path(self.folder.name));self.local.start()
        self.env=patch.dict('os.environ',{},clear=False);self.env.start()
        import os;os.environ.pop('GEMINI_API_KEY',None)

    def tearDown(self):
        self.env.stop();self.local.stop();self.folder.cleanup()

    def test_gemini_is_default_and_needs_key(self):
        status=analysis.settings_status()
        self.assertEqual(status['provider'],'gemini');self.assertEqual(status['model'],gemini_model.DEFAULT_MODEL)
        self.assertFalse(status['configured']);self.assertEqual(status['billing_mode'],'free_tier')
        with self.assertRaisesRegex(ValueError,'Gemini API key'):analysis.save_settings({})
        with patch.object(analysis,'build_context') as context:
            with self.assertRaisesRegex(ValueError,'Gemini API key'):analysis.generate('2025-11')
            context.assert_not_called()

    def test_saved_key_is_kept_when_switching_back(self):
        key='AIzaFixtureKeyForUnitTests0123456789ab'
        status=analysis.save_settings({'provider':'gemini','api_key':key})
        self.assertTrue(status['configured']);self.assertNotIn(key,json.dumps(status))
        analysis.save_settings({'provider':'codex'})
        self.assertTrue(analysis.save_settings({'provider':'gemini'})['configured'])
        self.assertEqual(analysis.settings()['api_key'],key)
        with self.assertRaises(ValueError):analysis.save_settings({'provider':'gemini','api_key':'bad key'})

    def test_request_and_response_without_live_call(self):
        response={'candidates':[{'content':{'parts':[{'text':json.dumps(SUMMARY)}]},'finishReason':'STOP'}],
                  'usageMetadata':{'promptTokenCount':40,'candidatesTokenCount':60,'thoughtsTokenCount':5},'modelVersion':'gemini-3.8-flash'}
        with patch.object(gemini_model,'urlopen',return_value=io.BytesIO(json.dumps(response).encode())) as post:
            summary,usage,model=analysis.ask_model({'target_month':'2026-11','source_ledger':SOURCES},{'provider':'gemini','api_key':'AIza-secret-test-key-000000','model':gemini_model.DEFAULT_MODEL})
        self.assertEqual(summary,SUMMARY);self.assertEqual(usage['output_tokens'],65);self.assertEqual(model,'gemini-3.8-flash')
        request=post.call_args.args[0]
        self.assertTrue(request.full_url.endswith('/models/gemini-3.8-flash:generateContent'))
        self.assertNotIn('AIza-secret',request.data.decode())
        self.assertEqual(json.loads(request.data)['generationConfig']['responseMimeType'],'application/json')
        self.assertIsNone(analysis.usage_cost(usage,model))

    def test_truncation_and_errors_do_not_archive_or_leak(self):
        cut={'candidates':[{'content':{'parts':[{'text':'{'}]},'finishReason':'MAX_TOKENS'}]}
        config={'provider':'gemini','api_key':'AIza-secret-test-key-000000','model':gemini_model.DEFAULT_MODEL}
        with patch.object(gemini_model,'urlopen',return_value=io.BytesIO(json.dumps(cut).encode())):
            with self.assertRaisesRegex(ValueError,'truncated'):analysis.ask_model({'source_ledger':SOURCES},config)
        error=HTTPError('https://generativelanguage.googleapis.com',429,'secret raw provider detail',{},None)
        with patch.object(gemini_model,'urlopen',side_effect=error):
            with self.assertRaises(ValueError) as caught:analysis.ask_model({'source_ledger':SOURCES},config)
        self.assertIn('free-tier',str(caught.exception));self.assertNotIn('secret',str(caught.exception))


if __name__=='__main__':unittest.main()
