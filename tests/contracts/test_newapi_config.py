import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.config_model import OpenMontageConfig


class ConfigContract(unittest.TestCase):
    def test_old_configuration_remains_disabled_and_environment_overrides_deployment(self):
        from tools._newapi.config import load_settings
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.yaml'
            path.write_text('llm:\n  provider: newapi\n  model: text-id\nnewapi:\n  base_url: https://gateway.example/prefix/v1/\n')
            with patch.dict(os.environ, {'NEW_API_KEY': 'test-only', 'NEW_API_BASE_URL': 'https://override.example/proxy'}, clear=True):
                settings = load_settings(path)
            self.assertEqual(settings.config.base_url, 'https://override.example/proxy/v1')
            self.assertEqual(settings.llm.model, 'text-id')
            self.assertNotIn('test-only', repr(settings))
            self.assertNotIn('test-only', settings.config.model_dump_json())
        self.assertEqual(OpenMontageConfig().newapi.base_url, '')

    def test_profile_selects_only_declared_capability_and_retains_zero_false(self):
        from lib.config_model import NewAPIConfig
        from tools._newapi.config import NewAPISettings
        from tools._newapi.models import resolve_model, merge_params, model_catalog
        settings = NewAPISettings(NewAPIConfig.model_validate({
            'base_url': 'https://gateway.example',
            'default_models': {'text_generation': 'arbitrary-id'},
            'models': {'arbitrary-id': {'capabilities': ['text_generation'], 'protocols': ['messages','responses'], 'operations': ['messages','responses'], 'supported_parameters': ['temperature','parallel_tool_calls'], 'defaults': {'temperature': .7}}}
        }), 'test-only')
        resolved = resolve_model(settings, 'text_generation')
        self.assertEqual(resolved.protocol, 'responses')
        self.assertEqual(merge_params(resolved, {'temperature': 0}, {'parallel_tool_calls': False}), {'temperature': 0, 'parallel_tool_calls': False})
        self.assertIn('arbitrary-id', model_catalog(settings, 'text_generation'))
        with self.assertRaises(ValueError):
            resolve_model(settings, 'video_generation', 'arbitrary-id')
        with self.assertRaises(ValueError):
            merge_params(resolved, {}, {'model': 'other'})

    def test_url_contract_and_secret_rejection(self):
        from lib.config_model import NewAPIConfig
        good = {'https://gateway.example': 'https://gateway.example/v1', 'https://gateway.example/v1/': 'https://gateway.example/v1', 'https://gateway.example/prefix/': 'https://gateway.example/prefix/v1', 'http://localhost:3000/base/v1': 'http://localhost:3000/base/v1'}
        for value, expected in good.items():
            self.assertEqual(NewAPIConfig(base_url=value).base_url, expected)
        for bad in ['gateway.example', 'ftp://gateway.example', 'https://test-secret@gateway.example', 'https://gateway.example?key=test-secret', 'https://gateway.example/#secret', 'http://public.example', 'http://localhost:bad', 'https://gateway.example/a/../b']:
            with self.assertRaises(ValueError) as raised:
                NewAPIConfig(base_url=bad)
            self.assertNotIn('test-secret', str(raised.exception))
        with self.assertRaises(ValueError) as raised:
            NewAPIConfig(api_key='test-secret')
        self.assertNotIn('test-secret', str(raised.exception))

    def test_models_refresh_is_explicit_permission_scoped_and_protocol_checked(self):
        from unittest.mock import Mock
        from lib.config_model import NewAPIConfig
        from tools._newapi.config import NewAPISettings
        from tools._newapi.models import model_catalog, refresh_models, resolve_model
        config = NewAPIConfig.model_validate({'base_url': 'https://scope.example', 'default_models': {'text_generation': 'visible'}, 'models': {'visible': {'capabilities': ['text_generation'], 'protocols': ['anthropic']}, 'other': {'capabilities': ['text_generation'], 'protocols': ['responses']}}})
        a, b = NewAPISettings(config, 'key-a'), NewAPISettings(config, 'key-b')
        client = Mock()
        client.request_json.return_value = {'success': True, 'data': [{'id':'visible','supported_endpoint_types':['openai-response']}]}
        self.assertEqual(set(model_catalog(a, 'text_generation')), {'visible','other'})
        refresh_models(a, client)
        self.assertEqual(set(model_catalog(a, 'text_generation')), {'visible'})
        self.assertEqual(set(model_catalog(b, 'text_generation')), {'visible','other'})
        with self.assertRaises(ValueError):
            resolve_model(a, 'text_generation', protocol='anthropic')
        with self.assertRaises(ValueError):
            resolve_model(a, 'text_generation', 'other')

    def test_generic_openai_catalog_retains_explicit_responses_protocol(self):
        from unittest.mock import Mock
        from lib.config_model import NewAPIConfig
        from tools._newapi.config import NewAPISettings
        from tools._newapi.models import refresh_models, resolve_model
        config = NewAPIConfig.model_validate({'base_url': 'https://generic-openai.example', 'default_llm_protocol': 'responses', 'default_models': {'text_generation': 'gateway-text'}, 'models': {'gateway-text': {'capabilities': ['text_generation'], 'protocols': ['responses'], 'operations': ['responses']}}})
        settings = NewAPISettings(config, 'catalog-test-key')
        client = Mock()
        client.request_json.return_value = {'data': [{'id': 'gateway-text', 'supported_endpoint_types': ['openai']}]}
        refresh_models(settings, client)
        resolved = resolve_model(settings, 'text_generation')
        self.assertEqual((resolved.id, resolved.protocol), ('gateway-text', 'responses'))

    def test_async_operation_limits_and_resume_validation(self):
        from lib.config_model import NewAPIConfig
        from tools._newapi.config import NewAPISettings
        from tools._newapi.models import make_job, validate_resume, persist_job, resolve_model, merge_params
        settings = NewAPISettings(NewAPIConfig.model_validate({'base_url': 'https://jobs.example', 'default_models': {'image_generation':'image'}, 'models': {'image': {'capabilities':['image_generation'], 'operations':['generate','async'], 'supports_async':True, 'supported_parameters':['n'], 'limits': {'n': {'type':'integer','minimum':1,'maximum':2}}}}}), 'test-secret')
        resolved = resolve_model(settings, 'image_generation', operation='generate', request_mode='async')
        with self.assertRaises(ValueError):
            resolve_model(settings, 'image_generation', operation='edit', request_mode='async')
        with self.assertRaises(ValueError):
            merge_params(resolved, {'n': 3})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'image.png'
            job = make_job(settings, tool='newapi_image', model='image', operation='generate', request_mode='async', id='id', output_path=path, params={'key':'test-secret','prompt':'private','n':1})
            written = Path(persist_job(job))
            self.assertNotIn('test-secret', written.read_text())
            self.assertNotIn('private', written.read_text())
            self.assertEqual(validate_resume(settings, job, tool='newapi_image')['id'], 'id')
            for changed in ({'tool':'newapi_video'}, {'base_url':'https://other.example'}, {'endpoint':'https://evil.example'}, {'output_path':42}):
                with self.assertRaises(ValueError):
                    validate_resume(settings, {**job, **changed}, tool='newapi_image')
            with self.assertRaises(ValueError):
                validate_resume(settings, job, tool='newapi_image', output_path=path.with_name('other.png'))

    def test_key_only_deployment_fixture_resolves_four_defaults(self):
        from tools._newapi.config import load_settings
        from tools._newapi.models import resolve_model
        path = Path(__file__).parents[1] / 'fixtures/newapi/deployment.yaml'
        with patch.dict(os.environ, {'NEW_API_KEY':'test-only'}, clear=True):
            settings = load_settings(path)
        for capability in ['text_generation','image_generation','video_generation','tts']:
            self.assertEqual(resolve_model(settings,capability,request_mode='async' if capability == 'video_generation' else 'sync').id,settings.config.default_models[capability])

    def test_auto_llm_protocol_uses_the_deployment_default_when_both_available(self):
        from lib.config_model import NewAPIConfig
        from tools._newapi.config import NewAPISettings
        from tools._newapi.models import resolve_model
        settings = NewAPISettings(NewAPIConfig.model_validate({'default_models':{'text_generation':'dual'}, 'models':{'dual':{'capabilities':['text_generation'],'protocols':['anthropic','responses']}}}), 'test-only')
        self.assertEqual(resolve_model(settings,'text_generation',protocol='auto').protocol,'responses')

    def test_job_identity_never_persists_or_rewrites_a_gateway_key(self):
        from lib.config_model import NewAPIConfig
        from tools._newapi.config import NewAPISettings
        from tools._newapi.models import make_job,validate_resume
        settings = NewAPISettings(NewAPIConfig.model_validate({'base_url':'https://job-secrets.example','models':{'image':{'capabilities':['image_generation'],'operations':['async'],'supports_async':True}}}), 'test-secret')
        common = {'tool':'newapi_image','model':'image','operation':'generate','request_mode':'async','id':'task-safe','output_path':'safe.png'}
        for overrides in [{'id':'task-test-secret'}, {'output_path':'test-secret.png'}, {'model':'test-secret'}]:
            with self.assertRaises(ValueError):
                make_job(settings,**{**common,**overrides})
        job = make_job(settings,**common)
        with self.assertRaises(ValueError):
            validate_resume(settings,{**job,'id':'task-test-secret'},tool='newapi_image')

    def test_invalid_deployment_defaults_or_schemas_cannot_advertise_models(self):
        from lib.config_model import NewAPIConfig
        for limits, default in [({'type':'number','maximum':4},99),({'type':'not-a-json-schema-type'},1)]:
            with self.assertRaises(ValueError) as raised:
                NewAPIConfig.model_validate({'models':{'speech':{'capabilities':['tts'],'supported_parameters':['speed'],'defaults':{'speed':default},'limits':{'speed':limits}}}})
            self.assertIn('speed',str(raised.exception))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.yaml'
            path.write_text('newapi:\n  base_url: https://invalid-profile.example\n  models:\n    text:\n      capabilities: [text_generation]\n      protocols: [responses]\n      operations: [responses]\n      supported_parameters: [temperature]\n      defaults: {temperature: 99}\n      limits:\n        temperature: {type: number, maximum: 1}\n')
            from tools.newapi_llm import NewAPILLM
            with patch.dict(os.environ,{'NEW_API_KEY':'test-only','NEW_API_BASE_URL':''},clear=True):
                tool = NewAPILLM(config_path=path)
                self.assertEqual(tool.get_status().value,'unavailable')
                self.assertEqual(tool.get_info()['model_catalog'],{})

    def test_nonfinite_http_and_poll_timeouts_are_invalid_deployment_values(self):
        from lib.config_model import NewAPIConfig
        for field in ['connect_timeout','read_timeout','poll_timeout','poll_interval']:
            for value in [float('inf'),float('nan')]:
                with self.assertRaises(ValueError):
                    NewAPIConfig(**{field:value})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.yaml'
            path.write_text('newapi:\n  read_timeout: .inf\n')
            from tools._newapi.config import load_settings
            with self.assertRaises(ValueError):
                load_settings(path)

    def test_parent_configuration_errors_never_echo_forbidden_yaml_secrets(self):
        from tools.newapi_llm import NewAPILLM
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.yaml'
            path.write_text('newapi:\n  api_key: different-secret\n')
            with patch.dict(os.environ,{'NEW_API_KEY':'test-only','NEW_API_BASE_URL':''},clear=True):
                result = NewAPILLM(config_path=path).execute({'messages':[{'role':'user','content':'Hello'}]})
                info = NewAPILLM(config_path=path).get_info()
            self.assertFalse(result.success)
            self.assertNotIn('different-secret',str(result))
            self.assertNotIn('different-secret',str(info))
