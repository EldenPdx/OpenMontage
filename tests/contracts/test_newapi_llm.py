"""Synthetic wire contracts from the audited upstream SHA; see fixture provenance."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests
import yaml


FIXTURES = Path(__file__).parents[1] / 'fixtures' / 'newapi'


def wire_response(payload, *, status=200, mime='application/json'):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode() if isinstance(payload, dict) else payload
    result._content_consumed = True
    result.headers['Content-Type'] = mime
    result.headers['x-request-id'] = 'req-synthetic'
    return result


class LLMContract(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config_path = Path(self.directory.name) / 'config.yaml'
        self.raw = yaml.safe_load((FIXTURES/'deployment.yaml').read_text())
        self.raw['newapi']['base_url'] = 'https://llm-tests.example/prefix'
        self.config_path.write_text(yaml.safe_dump(self.raw))
        self.environment = patch.dict(os.environ, {'NEW_API_KEY':'test-llm-key','NEW_API_BASE_URL':''}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def tool(self):
        from tools.newapi_llm import NewAPILLM
        return NewAPILLM(config_path=self.config_path)

    def test_responses_public_text_is_exact_wire_and_returns_all_output_blocks(self):
        payload = {'id':'resp-synthetic','object':'response','model':'deployment-text','status':'completed','output':[
            {'type':'reasoning','summary':[{'type':'summary_text','text':'private reasoning summary'}]},
            {'type':'message','role':'assistant','content':[{'type':'output_text','text':'{"scene":'}, {'type':'output_text','text':'1}'}]},
            {'type':'function_call','id':'fc1','call_id':'call1','name':'plan_scene','arguments':'{"scene":1}'}
        ],'usage':{'input_tokens':5,'output_tokens':12}}
        with patch('requests.Session.request',return_value=wire_response(payload)) as transport:
            result = self.tool().execute({'system':'Scene plan','messages':[{'role':'user','content':'Rain'}], 'temperature':0,'max_tokens':64})
        self.assertTrue(result.success, result.error)
        self.assertEqual(result.data['text'],'{"scene":1}')
        self.assertEqual(result.data['output'],payload['output'])
        self.assertEqual(result.data['tool_calls'],[payload['output'][2]])
        self.assertEqual(result.data['usage'],{'input_tokens':5,'output_tokens':12})
        self.assertIsNone(result.cost_usd)
        args, kwargs = transport.call_args
        self.assertEqual(args[:2],('POST','https://llm-tests.example/prefix/v1/responses'))
        self.assertEqual(kwargs['json'],{'model':'deployment-text','instructions':'Scene plan','input':[{'role':'user','content':'Rain'}],'temperature':0,'max_output_tokens':64,'stream':False})
        self.assertEqual(kwargs['headers'],{'Authorization':'Bearer test-llm-key'})
        self.assertEqual(transport.call_count,1)

    def test_messages_native_tool_results_and_multimodal_blocks_are_lossless(self):
        self.raw['newapi']['models']['deployment-text']['protocols'] = ['anthropic']
        self.raw['newapi']['models']['deployment-text']['supported_parameters'] += ['thinking']
        self.config_path.write_text(yaml.safe_dump(self.raw))
        native = {'system':[{'type':'text','text':'Scene planner','cache_control':{'type':'ephemeral'}}], 'messages':[{'role':'user','content':[
            {'type':'tool_result','tool_use_id':'tool1','content':'A rain scene','is_error':False},
            {'type':'image','source':{'type':'base64','media_type':'image/png','data':'synthetic'}}
        ]}], 'max_tokens':64, 'temperature':0,'thinking':{'type':'disabled'}}
        payload = {'id':'msg1','type':'message','role':'assistant','model':'deployment-text','content':[
            {'type':'thinking','thinking':'thought','signature':'sig'},
            {'type':'tool_use','id':'use1','name':'plan','input':{'scene':1}}
        ],'stop_reason':'tool_use','usage':{'input_tokens':10,'output_tokens':4}}
        with patch('requests.Session.request',return_value=wire_response(payload)) as transport:
            result = self.tool().execute({'request':native})
        self.assertTrue(result.success,result.error)
        self.assertEqual(result.data['text'],'')
        self.assertEqual(result.data['content'],payload['content'])
        self.assertEqual(result.data['tool_calls'],[payload['content'][1]])
        self.assertEqual(transport.call_args.args[:2],('POST','https://llm-tests.example/prefix/v1/messages'))
        self.assertEqual(transport.call_args.kwargs['headers'],{'Authorization':'Bearer test-llm-key','anthropic-version':'2023-06-01'})
        self.assertEqual(transport.call_args.kwargs['json'],{**native,'model':'deployment-text','stream':False})

    def test_native_tuning_cannot_conflict_with_public_max_tokens(self):
        with patch('requests.Session.request') as transport:
            result = self.tool().execute({'messages':[{'role':'user','content':'Rain'}], 'max_tokens':64,'request':{'max_output_tokens':128}})
        self.assertFalse(result.success)
        transport.assert_not_called()

    def test_messages_sse_aggregates_text_thinking_tool_fragments_and_usage(self):
        self.raw['newapi']['models']['deployment-text']['protocols'] = ['anthropic']
        self.config_path.write_text(yaml.safe_dump(self.raw))
        events = [
            ('message_start', {'type':'message_start','message':{'id':'msg-stream','model':'deployment-text','content':[],'usage':{'input_tokens':10,'output_tokens':1}}}),
            ('content_block_start', {'type':'content_block_start','index':0,'content_block':{'type':'text','text':''}}),
            ('content_block_delta', {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':'Rain'}}),
            ('content_block_delta', {'type':'content_block_delta','index':0,'delta':{'type':'text_delta','text':' scene'}}),
            ('content_block_stop', {'type':'content_block_stop','index':0}),
            ('content_block_start', {'type':'content_block_start','index':1,'content_block':{'type':'thinking','thinking':'','signature':''}}),
            ('content_block_delta', {'type':'content_block_delta','index':1,'delta':{'type':'thinking_delta','thinking':'private'}}),
            ('content_block_delta', {'type':'content_block_delta','index':1,'delta':{'type':'signature_delta','signature':'signed'}}),
            ('content_block_stop', {'type':'content_block_stop','index':1}),
            ('content_block_start', {'type':'content_block_start','index':2,'content_block':{'type':'tool_use','id':'use1','name':'plan','input':{}}}),
            ('content_block_delta', {'type':'content_block_delta','index':2,'delta':{'type':'input_json_delta','partial_json':'{"scene":'}}),
            ('content_block_delta', {'type':'content_block_delta','index':2,'delta':{'type':'input_json_delta','partial_json':'1}'}}),
            ('content_block_stop', {'type':'content_block_stop','index':2}),
            ('message_delta', {'type':'message_delta','delta':{'stop_reason':'tool_use'},'usage':{'output_tokens':12}}),
            ('message_stop', {'type':'message_stop'})
        ]
        body = b': heartbeat\r\n\r\n' + b''.join(('event: '+event+'\r\ndata: '+json.dumps(data)+'\r\n\r\n').encode() for event,data in events)
        with patch('requests.Session.request',return_value=wire_response(body,mime='text/event-stream')) as transport:
            result = self.tool().execute({'messages':[{'role':'user','content':'Rain'}],'stream':True})
        self.assertTrue(result.success,result.error)
        self.assertEqual(result.data['text'],'Rain scene')
        self.assertEqual(result.data['tool_calls'][0]['input'],{'scene':1})
        self.assertEqual(result.data['content'][1],{'type':'thinking','thinking':'private','signature':'signed'})
        self.assertEqual(result.data['usage'],{'input_tokens':10,'output_tokens':12})
        self.assertTrue(transport.call_args.kwargs['stream'])

    def test_responses_sse_aggregates_output_indexes_tools_and_terminal_alias(self):
        events = [
            {'type':'response.created','response':{'id':'resp-stream','model':'deployment-text','status':'in_progress','output':[]}},
            {'type':'response.output_item.added','output_index':0,'item':{'type':'message','id':'msg1','role':'assistant','content':[]}},
            {'type':'response.content_part.added','output_index':0,'content_index':0,'part':{'type':'output_text','text':''}},
            {'type':'response.output_text.delta','output_index':0,'content_index':0,'delta':'Rain'},
            {'type':'response.output_text.delta','output_index':0,'content_index':0,'delta':' scene'},
            {'type':'response.output_text.done','output_index':0,'content_index':0,'text':'Rain scene'},
            {'type':'response.output_item.added','output_index':1,'item':{'type':'function_call','id':'fc1','call_id':'call1','name':'plan','arguments':''}},
            {'type':'response.function_call_arguments.delta','output_index':1,'delta':'{"scene":'},
            {'type':'response.function_call_arguments.delta','output_index':1,'delta':'1}'},
            {'type':'response.function_call_arguments.done','output_index':1,'arguments':'{"scene":1}'},
            {'type':'response.done','response':{'id':'resp-stream','model':'deployment-text','status':'completed','usage':{'input_tokens':10,'output_tokens':12}}}
        ]
        # Multiline data and CRLF are protocol frames, not separate JSON objects.
        body = b''.join(('event: '+data['type']+'\r\ndata: '+json.dumps(data,indent=2).replace('\n','\r\ndata: ')+'\r\n\r\n').encode() for data in events)
        with patch('requests.Session.request',return_value=wire_response(body,mime='text/event-stream')) as transport:
            result = self.tool().execute({'messages':[{'role':'user','content':'Rain'}],'stream':True})
        self.assertTrue(result.success,result.error)
        self.assertEqual(result.data['text'],'Rain scene')
        self.assertEqual(len(result.data['output']),2)
        self.assertEqual(result.data['tool_calls'][0]['arguments'],'{"scene":1}')
        self.assertEqual(result.data['usage'],{'input_tokens':10,'output_tokens':12})
        self.assertEqual(transport.call_count,1)

    def test_malformed_json_and_missing_stream_terminal_have_unknown_outcome(self):
        examples = [
            (b'{invalid', 'application/json', False),
            (b'data: [DONE]\n\n','text/event-stream',True),
            (b'event: response.created\ndata: {"type":"response.created","response":{"status":"in_progress","output":[]}}\n\n','text/event-stream',True),
            (b'event: response.completed\ndata: {not json}\n\n','text/event-stream',True)
        ]
        for body,mime,stream in examples:
            with patch('requests.Session.request',return_value=wire_response(body,mime=mime)) as transport:
                result = self.tool().execute({'messages':[{'role':'user','content':'Rain'}],'stream':stream})
            self.assertFalse(result.success)
            self.assertTrue(result.data['error']['outcome_unknown'],result.data)
            self.assertEqual(transport.call_count,1)
            self.assertNotIn('resume_job',result.data)

    def test_background_websocket_overrides_and_wrong_protocol_never_post(self):
        attempts = [
            {'background':False}, {'websocket':True}, {'transport':'websocket'},
            {'request':{'background':True}}, {'request':{'model':'other'}}, {'request':{'stream':True}},
            {'provider_params':{'model':'other'}}, {'provider_params':{'stream':True}}, {'provider_params':{'url':'https://evil.example'}},
            {'request':{'protocol':'anthropic'}}, {'request':{'headers':{'Authorization':'secret'}}}
        ]
        for extra in attempts:
            with patch('requests.Session.request') as transport:
                result = self.tool().execute({'messages':[{'role':'user','content':'Rain'}],**extra})
            self.assertFalse(result.success,extra)
            transport.assert_not_called()
        self.raw['newapi']['models']['deployment-text']['protocols'] = ['anthropic']
        self.config_path.write_text(yaml.safe_dump(self.raw))
        with patch('requests.Session.request') as transport:
            result = self.tool().execute({'protocol':'responses','messages':[{'role':'user','content':'Rain'}]})
        self.assertFalse(result.success)
        transport.assert_not_called()

    def test_incomplete_refusal_and_failed_results_report_reasons(self):
        examples = [
            {'status':'incomplete','incomplete_details':{'reason':'max_output_tokens'},'output':[{'type':'message','content':[{'type':'output_text','text':'partial'}]}]},
            {'status':'completed','output':[{'type':'message','content':[{'type':'refusal','refusal':'Cannot help'}]}]},
            {'status':'failed','error':{'code':'overloaded','message':'try later'},'output':[]}
        ]
        for payload in examples:
            with patch('requests.Session.request',return_value=wire_response(payload)) as transport:
                result = self.tool().execute({'messages':[{'role':'user','content':'Rain'}]})
            self.assertFalse(result.success)
            self.assertTrue(result.error)
            self.assertEqual(transport.call_count,1)
        self.assertEqual(result.data['error']['code'],'overloaded')

    def test_invalid_tool_arguments_do_not_report_complete_success(self):
        payload = {'status':'completed','output':[{'type':'function_call','name':'plan','call_id':'c1','arguments':'{"broken":'}]}
        with patch('requests.Session.request',return_value=wire_response(payload)):
            result = self.tool().execute({'messages':[{'role':'user','content':'Rain'}]})
        self.assertFalse(result.success)
        self.assertEqual(result.data['error']['code'],'invalid_response')

    def test_offline_registry_metadata_advertises_only_configured_protocol_operations(self):
        from tools.tool_registry import ToolRegistry
        from tools.provider_pricing import PriceQuoteRequired
        self.raw['newapi']['models']['deployment-text']['protocols'] = ['anthropic']
        self.raw['newapi']['models']['deployment-text']['operations'] = ['messages']
        self.config_path.write_text(yaml.safe_dump(self.raw))
        tool = self.tool()
        registry = ToolRegistry()
        registry.register(tool)
        with patch('requests.Session.request',side_effect=AssertionError('metadata opened network')) as transport:
            self.assertEqual(registry.get('newapi_llm'),tool)
            self.assertEqual(tool.get_status().value,'available')
            self.assertEqual(tool.get_info()['capabilities'],['messages'])
            self.assertEqual(set(tool.get_info()['model_catalog']),{'deployment-text'})
            self.assertIsNone(tool.dry_run({})['estimated_cost_usd'])
            with self.assertRaises(PriceQuoteRequired):
                tool.estimate_cost({})
        transport.assert_not_called()

    def test_public_text_file_and_native_arguments_are_not_truncated_or_double_encoded(self):
        text = '{"plan":"'+ '雨' * 1300 + ' https://example.org/page?section=one"}'
        arguments = {'count':0,'enabled':False,'text':text}
        payload = {'status':'completed','output':[
            {'type':'message','content':[{'type':'output_text','text':text}]},
            {'type':'function_call','name':'plan','call_id':'c1','arguments':arguments}
        ],'usage':{'output_tokens':2048}}
        output = Path(self.directory.name)/'scene-plan.json'
        native = {'input':[{'type':'function_call_output','call_id':'c0','output':'{"success":true}'}], 'tools':[{'type':'function','name':'plan','parameters':{'type':'object','properties':{}}}]}
        with patch('requests.Session.request',return_value=wire_response(payload)) as transport:
            result = self.tool().execute({'request':native,'output_path':str(output)})
        self.assertTrue(result.success,result.error)
        self.assertEqual(result.data['text'],text)
        self.assertEqual(result.data['tool_calls'][0]['arguments'],arguments)
        self.assertEqual(output.read_text(),text)
        self.assertEqual(transport.call_args.kwargs['json']['input'],native['input'])
        self.assertEqual(transport.call_args.kwargs['json']['tools'],native['tools'])

    def test_invalid_token_limits_are_rejected_before_paid_requests(self):
        for value in [0,-1,True,'64']:
            with patch('requests.Session.request') as transport:
                result = self.tool().execute({'messages':[{'role':'user','content':'Rain'}],'max_tokens':value})
            self.assertFalse(result.success)
            transport.assert_not_called()

    def test_responses_stream_reasoning_refusal_and_incomplete_retain_native_output(self):
        events = [
            {'type':'response.created','response':{'id':'reasoning','model':'deployment-text','status':'in_progress','output':[]}},
            {'type':'response.output_item.added','output_index':0,'item':{'type':'reasoning','id':'rs1','summary':[]}},
            {'type':'response.reasoning_summary_part.added','output_index':0,'summary_index':0,'part':{'type':'summary_text','text':''}},
            {'type':'response.reasoning_summary_text.delta','item_id':'rs1','summary_index':0,'delta':'Thought summary'},
            {'type':'response.reasoning_summary_text.done','output_index':0,'summary_index':0,'text':'Thought summary'},
            {'type':'response.output_item.added','output_index':1,'item':{'type':'message','id':'m1','content':[]}},
            {'type':'response.content_part.added','output_index':1,'content_index':0,'part':{'type':'refusal','refusal':''}},
            {'type':'response.refusal.delta','output_index':1,'content_index':0,'delta':'Cannot '},
            {'type':'response.refusal.done','output_index':1,'content_index':0,'refusal':'Cannot help'},
            {'type':'response.completed','response':{'status':'completed','usage':{'output_tokens':0}}}
        ]
        body = b''.join(('event: '+data['type']+'\ndata: '+json.dumps(data)+'\n\n').encode() for data in events)
        with patch('requests.Session.request',return_value=wire_response(body,mime='text/event-stream')):
            result = self.tool().execute({'messages':[{'role':'user','content':'Rain'}],'stream':True})
        self.assertFalse(result.success)
        self.assertEqual(result.data['status'],'refused')
        self.assertEqual(result.data['text'],'')
        self.assertEqual(result.data['output'][0]['summary'][0]['text'],'Thought summary')
        self.assertEqual(result.data['output'][1]['content'][0]['refusal'],'Cannot help')
        self.assertEqual(result.data['usage'],{'output_tokens':0})

    def test_conflicting_terminal_event_cannot_turn_failure_into_success(self):
        event = {'type':'response.failed','response':{'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'looks complete'}]}]}}
        body = ('event: response.failed\ndata: '+json.dumps(event)+'\n\n').encode()
        with patch('requests.Session.request',return_value=wire_response(body,mime='text/event-stream')):
            result = self.tool().execute({'messages':[{'role':'user','content':'Rain'}],'stream':True})
        self.assertFalse(result.success)
        self.assertTrue(result.data['error']['outcome_unknown'])

    def test_stream_error_redacts_key_and_sensitive_url_without_retry(self):
        body = b'event: error\ndata: {"type":"error","error":{"code":"denied","message":"test-llm-key https://gateway.example/error?token=private"}}\n\n'
        with patch('requests.Session.request',return_value=wire_response(body,mime='text/event-stream')) as transport:
            result = self.tool().execute({'messages':[{'role':'user','content':'Rain'}],'stream':True})
        self.assertFalse(result.success)
        self.assertEqual(result.data['error']['code'],'denied')
        self.assertNotIn('test-llm-key',json.dumps(result.data))
        self.assertNotIn('private',json.dumps(result.data))
        self.assertEqual(transport.call_count,1)

    def test_messages_stream_without_message_stop_cannot_report_success(self):
        self.raw['newapi']['models']['deployment-text']['protocols'] = ['anthropic']
        self.config_path.write_text(yaml.safe_dump(self.raw))
        events = [
            {'type':'message_start','message':{'content':[],'model':'deployment-text'}},
            {'type':'message_delta','delta':{'stop_reason':'end_turn'},'usage':{'output_tokens':1}}
        ]
        body = b''.join(('event: '+data['type']+'\ndata: '+json.dumps(data)+'\n\n').encode() for data in events)
        with patch('requests.Session.request',return_value=wire_response(body,mime='text/event-stream')):
            result = self.tool().execute({'messages':[{'role':'user','content':'Rain'}],'stream':True})
        self.assertFalse(result.success)
        self.assertTrue(result.data['error']['outcome_unknown'])

    def test_sync_and_sse_fixture_results_are_identical_for_both_protocols(self):
        for protocol,name in [('anthropic','messages'),('responses','responses')]:
            wire = json.loads((FIXTURES/f'llm_{name}.json').read_text())
            stream = (FIXTURES/f'llm_{name}.sse').read_bytes()
            outputs = []
            for use_stream,payload,mime in [(False,wire,'application/json'),(True,stream,'text/event-stream')]:
                with patch('requests.Session.request',return_value=wire_response(payload,mime=mime)):
                    result = self.tool().execute({'protocol':protocol,'messages':[{'role':'user','content':'Rain'}],'stream':use_stream})
                self.assertTrue(result.success,result.error)
                outputs.append(result.data)
            self.assertEqual(outputs[0],outputs[1])

    def test_success_redacts_echoed_gateway_key_and_failure_preserves_output_file(self):
        output = Path(self.directory.name)/'existing.json'
        output.write_text('previous approved plan')
        payload = {'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'test-llm-key is echoed'}]}]}
        with patch('requests.Session.request',return_value=wire_response(payload)):
            result = self.tool().execute({'messages':[{'role':'user','content':'Rain'}]})
        self.assertTrue(result.success,result.error)
        self.assertNotIn('test-llm-key',json.dumps(result.data))
        payload = {'status':'incomplete','output':[],'incomplete_details':{'reason':'max_output_tokens'}}
        with patch('requests.Session.request',return_value=wire_response(payload)):
            result = self.tool().execute({'messages':[{'role':'user','content':'Rain'}],'output_path':str(output)})
        self.assertFalse(result.success)
        self.assertEqual(output.read_text(),'previous approved plan')

    def test_unknown_public_and_provider_content_fields_cannot_be_silently_dropped(self):
        self.raw['newapi']['models']['deployment-text']['supported_parameters'] += ['input']
        self.config_path.write_text(yaml.safe_dump(self.raw))
        for extra in [{'tools':[]},{'max_output_tokens':64},{'model_name':'wrong'},{'provider_params':{'input':'different input'}}]:
            with patch('requests.Session.request') as transport:
                result = self.tool().execute({'messages':[{'role':'user','content':'Rain'}],**extra})
            self.assertFalse(result.success)
            transport.assert_not_called()

    def test_pure_tool_call_does_not_replace_an_existing_text_artifact_with_empty_text(self):
        output = Path(self.directory.name)/'existing.txt'
        output.write_text('approved plan')
        payload = {'status':'completed','output':[{'type':'function_call','name':'plan','call_id':'c1','arguments':{'scene':1}}]}
        with patch('requests.Session.request',return_value=wire_response(payload)):
            result = self.tool().execute({'messages':[{'role':'user','content':'Rain'}],'output_path':str(output)})
        self.assertTrue(result.success,result.error)
        self.assertEqual(output.read_text(),'approved plan')
        self.assertEqual(result.artifacts,[])

    def test_incomplete_errors_have_safe_shared_structure(self):
        payload = {'status':'incomplete','output':[],'incomplete_details':{'reason':'https://private.example/detail?token=private'}}
        with patch('requests.Session.request',return_value=wire_response(payload)):
            result = self.tool().execute({'messages':[{'role':'user','content':'Rain'}]})
        self.assertFalse(result.success)
        self.assertEqual(result.data['error']['code'],'llm_incomplete')
        self.assertNotIn('token=private',json.dumps(result.data))

    def test_semantic_key_token_secret_and_headers_tool_arguments_are_lossless(self):
        self.raw['newapi']['models']['deployment-text']['protocols'] = ['anthropic']
        self.config_path.write_text(yaml.safe_dump(self.raw))
        arguments = {'key':'article-key','token':'semantic-word','secret':'plot twist','headers':['chapter one'],'echo':'test-llm-key'}
        payload = {'stop_reason':'tool_use','content':[{'type':'tool_use','id':'use1','name':'plan','input':arguments}]}
        with patch('requests.Session.request',return_value=wire_response(payload)):
            result = self.tool().execute({'messages':[{'role':'user','content':'Rain'}]})
        self.assertTrue(result.success,result.error)
        self.assertEqual(result.data['tool_calls'][0]['input'],{**arguments,'echo':'[redacted]'})
        self.assertEqual(result.data['content'][0]['input'],{**arguments,'echo':'[redacted]'})
