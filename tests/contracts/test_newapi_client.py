import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from lib.config_model import NewAPIConfig
from tools._newapi.config import NewAPISettings


class Handler(BaseHTTPRequestHandler):
    posts = 0
    def log_message(self, *args):
        pass
    def do_POST(self):
        type(self).posts += 1
        self.rfile.read(int(self.headers.get('Content-Length', '0')))
        body = json.dumps({'error': {'message': 'bad test-secret https://cdn.example/out?token=private', 'code': 'bad_model'}, 'request_id': 'req-safe'}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ClientContract(unittest.TestCase):
    def test_paid_post_is_once_and_business_error_is_safe(self):
        from tools._newapi.client import NewAPIClient, NewAPIError
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        Handler.posts = 0
        settings = NewAPISettings(NewAPIConfig(base_url=f'http://127.0.0.1:{server.server_port}'), 'test-secret')
        try:
            with self.assertRaises(NewAPIError) as raised:
                NewAPIClient(settings).request_json('POST', '/v1/responses', json={'model': 'x'})
            self.assertEqual(Handler.posts, 1)
            self.assertEqual(raised.exception.request_id, 'req-safe')
            self.assertNotIn('test-secret', str(raised.exception))
            self.assertNotIn('private', str(raised.exception))
        finally:
            server.shutdown()
            server.server_close()

    def test_post_disconnect_never_retries_and_invalid_json_is_outcome_unknown(self):
        from unittest.mock import Mock
        from tools._newapi.client import NewAPIClient, NewAPIError
        session = Mock()
        session.request.side_effect = requests.ConnectionError('Authorization: Bearer test-secret')
        client = NewAPIClient(NewAPISettings(NewAPIConfig(base_url='https://gateway.example'), 'test-secret'), session)
        with self.assertRaises(NewAPIError) as raised:
            client.request_json('POST', '/v1/responses')
        self.assertTrue(raised.exception.outcome_unknown)
        self.assertEqual(session.request.call_count, 1)
        session.request.side_effect = None
        response = requests.Response()
        response.status_code, response._content = 200, b'not JSON'
        session.request.return_value = response
        with self.assertRaises(NewAPIError) as raised:
            client.request_json('POST', '/v1/responses')
        self.assertTrue(raised.exception.outcome_unknown)

    def test_task_ids_cannot_be_resume_urls_or_relative_paths(self):
        from tools._newapi.client import task_path, NewAPIClient, NewAPIError
        client = NewAPIClient(NewAPISettings(NewAPIConfig(base_url='https://gateway.example'), 'test-secret'))
        self.assertEqual(task_path('videos', 'task_123', True), '/v1/videos/task_123/content')
        for bad in ['..','.', 'https://evil.example', 'a/b','a\\b','a?token=x','a#x','a%2fb']:
            with self.assertRaises(ValueError):
                task_path('tasks', bad)
        with self.assertRaises(NewAPIError):
            client.request_json('GET', '/v1/tasks/id/content')

    def test_get_retry_after_honors_deadline_and_terminal_codes(self):
        from unittest.mock import Mock, patch
        from tools._newapi.client import NewAPIClient, NewAPIError
        def response(status, payload, headers=None):
            value = requests.Response()
            value.status_code, value._content = status, json.dumps(payload).encode()
            value.headers.update(headers or {})
            return value
        clock = [100.]
        session = Mock()
        session.request.side_effect = [response(429, {'error':{'message':'wait'}}, {'Retry-After':'3'}), response(200, {'data':[]})]
        client = NewAPIClient(NewAPISettings(NewAPIConfig(base_url='https://gateway.example'), 'test-secret'), session)
        with patch('time.monotonic', side_effect=lambda:clock[0]), patch('time.sleep', side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)):
            self.assertEqual(client.request_json('GET','/v1/models', deadline=105), {'data':[]})
        self.assertEqual(clock[0], 103.)
        self.assertLessEqual(session.request.call_args.kwargs['timeout'].total, 2)
        session.request.side_effect = [response(429, {'error':{'message':'wait'}}, {'Retry-After':'20'})]
        with patch('time.monotonic', return_value=100.), self.assertRaises(NewAPIError) as raised:
            client.request_json('GET','/v1/models', deadline=105)
        self.assertEqual(raised.exception.code, 'deadline_exceeded')
        for status, code in [(400,'bad'),(401,'bad'),(403,'bad'),(404,'bad'),(410,'bad'),(500,'result_data_unavailable')]:
            session.request.reset_mock()
            session.request.side_effect = [response(status, {'error':{'message':'terminal', 'code':code}})]
            with self.assertRaises(NewAPIError):
                client.request_json('GET','/v1/models')
            self.assertEqual(session.request.call_count, 1)

    def test_claude_failure_and_nonjson_errors_are_safe(self):
        from unittest.mock import Mock
        from tools._newapi.client import NewAPIClient, NewAPIError
        session = Mock()
        client = NewAPIClient(NewAPISettings(NewAPIConfig(base_url='https://gateway.example', get_retries=0), 'test-secret'), session)
        for status, body in [(200, {'type':'error','error':{'type':'invalid_request_error','message':'bad'}}), (200, {'success':False,'message':'denied'}), (200, {'status':'failed','error':{'message':'failed'}}), (502,None)]:
            response = requests.Response()
            response.status_code, response._content = status, json.dumps(body).encode() if body is not None else b'<html>test-secret private</html>'
            session.request.return_value = response
            with self.assertRaises(NewAPIError) as raised:
                client.request_json('GET','/v1/models')
            self.assertNotIn('test-secret', str(raised.exception))
            self.assertNotIn('private', str(raised.exception))

    def test_sse_rejects_wrong_content_type_and_preserves_frames(self):
        from tools._newapi.client import read_sse, NewAPIError
        response = requests.Response()
        response.status_code, response._content, response._content_consumed = 200, b'event: content_block_delta\ndata: {"value":0}\n\ndata: [DONE]\n\n', True
        response.headers['Content-Type'] = 'text/event-stream'
        self.assertEqual(list(read_sse(response)), [('content_block_delta',{'value':0}), ('message','[DONE]')])
        response = requests.Response()
        response.status_code, response._content, response._content_consumed = 200, b'data: {"value":0}\n\n', True
        response.headers['Content-Type'] = 'application/json'
        with self.assertRaises(NewAPIError):
            list(read_sse(response))

    def test_binary_download_is_atomic_and_rejects_empty_truncated_and_errors(self):
        import io
        import tempfile
        from pathlib import Path
        from PIL import Image
        from unittest.mock import Mock
        from tools._newapi.client import NewAPIClient, NewAPIError
        image = io.BytesIO()
        Image.new('RGB',(2,2),'red').save(image,format='PNG')
        body = image.getvalue()
        client = NewAPIClient(NewAPISettings(NewAPIConfig(base_url='https://gateway.example'), 'test-secret'))
        def response(body, mime='image/png', length=None):
            value = requests.Response()
            value.status_code, value._content, value._content_consumed = 200, body, True
            value.headers['Content-Type'] = mime
            if length is not None:
                value.headers['Content-Length'] = str(length)
            return value
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'image.png'
            output.write_bytes(b'original')
            for value in [response(b''),response(body,length=len(body)+1),response(body[:20]),response(b'{"error":{"message":"denied"}}','application/json'),response(b'data: foo','text/event-stream')]:
                with self.assertRaises(NewAPIError):
                    client.write_binary(value,output,kind='image')
                self.assertEqual(output.read_bytes(),b'original')
                self.assertEqual(list(Path(directory).glob('*.part')),[])
            value = response(body)
            with self.assertRaises(NewAPIError):
                client.write_binary(value,output,kind='image',max_bytes=5)
            self.assertEqual(client.write_binary(response(body),output,kind='image'),str(output))
            self.assertEqual(output.read_bytes(),body)
            value = response(body)
            def interrupted(**kwargs):
                yield body[:10]
                raise requests.ConnectionError('test-secret')
            value.iter_content = interrupted
            value._newapi_paid = True
            with self.assertRaises(NewAPIError) as raised:
                client.write_binary(value,output,kind='image')
            self.assertTrue(raised.exception.outcome_unknown)
            self.assertEqual(output.read_bytes(),body)

    def test_cdn_redirect_receives_no_gateway_credential(self):
        import io
        import tempfile
        from pathlib import Path
        from PIL import Image
        from unittest.mock import Mock, patch
        from tools._newapi.client import NewAPIClient, task_path
        image = io.BytesIO()
        Image.new('RGB',(2,2),'blue').save(image,format='PNG')
        redirect = requests.Response()
        redirect.status_code, redirect._content, redirect._content_consumed = 302, b'', True
        redirect.headers['Location'] = 'https://cdn.example/output?signature=private'
        media = requests.Response()
        media.status_code, media._content, media._content_consumed = 200, image.getvalue(), True
        media.headers['Content-Type'] = 'image/png'
        gateway, anonymous = Mock(), Mock()
        gateway.request.return_value = redirect
        anonymous.__enter__ = Mock(return_value=anonymous)
        anonymous.__exit__ = Mock(return_value=False)
        anonymous.get.return_value = media
        client = NewAPIClient(NewAPISettings(NewAPIConfig(base_url='https://gateway.example'), 'test-secret'),gateway)
        with tempfile.TemporaryDirectory() as directory, patch('requests.Session',return_value=anonymous):
            client.download(task_path('videos','id',True),Path(directory)/'image.png',kind='image')
        self.assertEqual(gateway.request.call_args.kwargs['headers']['Authorization'],'Bearer test-secret')
        self.assertNotIn('Authorization',anonymous.get.call_args.kwargs.get('headers',{}))
        self.assertNotIn('test-secret',str(anonymous.get.call_args))

    def test_base64_media_uses_same_size_and_validator_contract(self):
        import base64
        import io
        import tempfile
        from pathlib import Path
        from PIL import Image
        from tools._newapi.client import NewAPIClient, NewAPIError
        image = io.BytesIO()
        Image.new('RGB',(2,2),'red').save(image,format='PNG')
        url = 'data:image/png;base64,' + base64.b64encode(image.getvalue()).decode()
        client = NewAPIClient(NewAPISettings(NewAPIConfig(base_url='https://gateway.example'), 'test-secret'))
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)/'image.png'
            with self.assertRaises(NewAPIError):
                client.download_media(url,output,kind='image',max_bytes=5)
            self.assertFalse(output.exists())
            def reject(path):
                raise NewAPIError('invalid_media','wrong format')
            with self.assertRaises(NewAPIError):
                client.download_media(url,output,kind='image',validator=reject)
            self.assertFalse(output.exists())

    def test_general_redaction_preserves_text_and_safe_url_ports(self):
        from tools._newapi.client import redact, safe_message
        text = 'x' * 3000
        self.assertEqual(redact(text,'test-secret'), text)
        self.assertEqual(redact('http://127.0.0.1:3000/v1','test-secret'),'http://127.0.0.1:3000/v1')
        self.assertEqual(safe_message('https://user:password@gateway.example:443/media?token=test-secret','test-secret'),'https://gateway.example:443/media')
        self.assertEqual(redact('https://gateway.example:443/media?foo=bar','test-secret'),'https://gateway.example:443/media?foo=bar')

    def test_pcm_requires_an_explicit_frame_validator_before_replace(self):
        import tempfile
        from pathlib import Path
        from tools._newapi.client import NewAPIClient, NewAPIError
        client = NewAPIClient(NewAPISettings(NewAPIConfig(base_url='https://gateway.example'), 'test-secret'))
        def response():
            value = requests.Response()
            value.status_code, value._content, value._content_consumed = 200,b'\x00\x00'*240,True
            value.headers['Content-Type'] = 'application/octet-stream'
            return value
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'audio.pcm'
            with self.assertRaises(NewAPIError):
                client.write_binary(response(),path,kind='pcm')
            def validate_frames(path):
                if path.stat().st_size != 480:
                    raise ValueError('Expected 240 mono 16-bit PCM samples')
            self.assertEqual(client.write_binary(response(),path,kind='pcm',validator=validate_frames),str(path))

    def test_credential_values_are_redacted_even_when_echoed_as_native_dictionary_keys(self):
        from tools._newapi.client import redact
        data = {'test-secret':{'token':'semantic-token','echo':'test-secret'}}
        self.assertEqual(redact(data,'test-secret',drop_credentials=False), {'[redacted]':{'token':'semantic-token','echo':'[redacted]'}})

    def test_explicit_task_receipts_keep_a_failed_public_id_without_weakening_errors(self):
        from unittest.mock import Mock
        from tools._newapi.client import NewAPIClient,NewAPIError
        settings = NewAPISettings(NewAPIConfig(base_url='https://receipt.example'), 'test-secret')
        session = Mock()
        client = NewAPIClient(settings,session)
        def response(payload,status=200):
            value = requests.Response()
            value.status_code,value._content = status,json.dumps(payload).encode()
            return value
        failed = {'id':'task-failed','object':'video','status':'failed','error':{'code':'vendor_rejected','message':'rejected'}}
        session.request.return_value = response(failed)
        self.assertEqual(client.request_json('POST','/v1/videos',receipt=True),failed)
        self.assertEqual(session.request.call_count,1)
        for path,payload,status,receipt in [('/v1/videos',failed,200,False),('/v1/videos',{'error':{'message':'denied'}},200,True),('/v1/videos',failed,500,True),('/v1/responses',failed,200,True)]:
            session.request.return_value = response(payload,status)
            with self.assertRaises(NewAPIError):
                client.request_json('POST',path,receipt=receipt)

    def test_prepared_requests_ignore_netrc_for_gateway_and_cdn_without_disabling_proxy(self):
        import io
        import tempfile
        from pathlib import Path
        from PIL import Image
        from unittest.mock import patch
        from tools._newapi.client import NewAPIClient
        class AuthenticationHandler(BaseHTTPRequestHandler):
            seen = []
            def log_message(self,*args):
                pass
            def do_GET(self):
                type(self).seen.append(self.headers.get('Authorization'))
                body = b'{"data":[]}'
                self.send_response(200)
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1',0),AuthenticationHandler)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        client = NewAPIClient(NewAPISettings(NewAPIConfig(base_url=f'http://127.0.0.1:{server.server_port}'),'test-secret'))
        try:
            with patch('requests.sessions.get_netrc_auth',return_value=('netrc-user','test-secret')):
                self.assertEqual(client.request_json('GET','/v1/models'),{'data':[]})
            self.assertEqual(AuthenticationHandler.seen,['Bearer test-secret'])
            self.assertTrue(client.session.trust_env)
        finally:
            server.shutdown()
            server.server_close()
        buffer = io.BytesIO()
        Image.new('RGB',(2,2),'red').save(buffer,format='PNG')
        headers = []
        def send(session,request,**kwargs):
            headers.append(dict(request.headers))
            self.assertTrue(session.trust_env)
            value = requests.Response()
            value.status_code,value._content,value._content_consumed = 200,buffer.getvalue(),True
            value.headers['Content-Type'] = 'image/png'
            return value
        with tempfile.TemporaryDirectory() as directory,patch('requests.sessions.get_netrc_auth',return_value=('netrc-user','test-secret')),patch('requests.Session.send',new=send):
            client.download_media('https://cdn.example/image.png',Path(directory)/'image.png')
        self.assertNotIn('Authorization',headers[0])

    def test_interrupted_json_error_body_does_not_hide_unknown_paid_outcome(self):
        from unittest.mock import Mock
        from tools._newapi.client import NewAPIClient,NewAPIError
        response = Mock(spec=requests.Response)
        response.status_code = 200
        response.headers = {'Content-Type':'application/json'}
        response.json.side_effect = requests.exceptions.ChunkedEncodingError('connection lost')
        session = Mock()
        session.request.return_value = response
        client = NewAPIClient(NewAPISettings(NewAPIConfig(base_url='https://json-interruption.example'),'test-secret'),session)
        with self.assertRaises(NewAPIError) as raised:
            client.request('POST','/v1/audio/speech',stream=True)
        self.assertTrue(raised.exception.outcome_unknown)
        self.assertEqual(session.request.call_count,1)
