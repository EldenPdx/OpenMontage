"""Messages/Responses codecs. Return tool calls to the agent; never execute them."""
from copy import deepcopy
import json

from tools._newapi.client import NewAPIError
from tools._newapi.models import merge_params, resolve_model


_NATIVE_CONTENT = {'anthropic': {'system', 'messages'}, 'responses': {'instructions', 'input'}}


def build_request(settings, inputs):
    if not isinstance(inputs, dict):
        raise ValueError('LLM inputs must be an object')
    allowed = {'model','protocol','system','messages','request','temperature','max_tokens','stream','provider_params','output_path','scene_id','project_dir','task_context'}
    if set(inputs) - allowed:
        raise ValueError('Unsupported LLM public fields: '+', '.join(sorted(set(inputs)-allowed)))
    if any(field in inputs for field in ('background', 'websocket', 'transport', 'resume_job', 'base_url', 'url', 'api_key', 'headers')):
        raise ValueError('Background, WebSocket, resume and transport overrides are unsupported')
    resolved = resolve_model(settings, 'text_generation', inputs.get('model'), inputs.get('protocol', 'auto'))
    protocol = resolved.protocol
    operation = 'messages' if protocol == 'anthropic' else 'responses'
    if operation not in resolved.profile.operations:
        raise ValueError(f'Deployment profile does not allow {operation}')
    native = inputs.get('request')
    if native is not None and not isinstance(native, dict):
        raise ValueError('Native request must be an object')
    native = deepcopy(native or {})
    stream = inputs.get('stream', False)
    if not isinstance(stream, bool):
        raise ValueError('stream must be a boolean')
    for field, expected in (('model', resolved.id), ('stream', stream)):
        if field in native and native.pop(field) != expected:
            raise ValueError(f'Native request conflicts with {field}')
    if any(field in native for field in ('protocol', 'operation', 'background', 'websocket', 'transport', 'url', 'base_url', 'endpoint', 'api_key', 'headers', 'authorization')):
        raise ValueError('Native request cannot override protocol, transport or credentials')
    content = {key: native.pop(key) for key in list(native) if key in _NATIVE_CONTENT[protocol]}
    if isinstance(inputs.get('provider_params'),dict) and set(inputs['provider_params']) & {'system','messages','input','instructions'}:
        raise ValueError('LLM content belongs in public text fields or native request, not provider_params')
    for public, wire in (('system', 'system' if protocol == 'anthropic' else 'instructions'), ('messages', 'messages' if protocol == 'anthropic' else 'input')):
        if public in inputs:
            value = inputs[public]
            if public == 'system' and not isinstance(value, str):
                raise ValueError('Public system must be text; use native request for blocks')
            if public == 'messages':
                if not isinstance(value, list) or not value or any(not isinstance(message, dict) or set(message) - {'role', 'content'} or message.get('role') not in {'user', 'assistant'} or not isinstance(message.get('content'), str) for message in value):
                    raise ValueError('Public messages must contain user/assistant text; use native request for tool results or media')
            if wire in content and content[wire] != value:
                raise ValueError(f'Public {public} conflicts with native {wire}')
            content[wire] = deepcopy(value)
    required = 'messages' if protocol == 'anthropic' else 'input'
    if required not in content or content[required] in (None, '', []):
        raise ValueError(f'LLM {protocol} request requires {required}')
    tuning = {}
    if settings.llm.provider == 'newapi':
        for key, value in [('temperature',settings.llm.temperature), ('max_tokens' if protocol == 'anthropic' else 'max_output_tokens',settings.llm.max_tokens)]:
            if key in resolved.profile.supported_parameters:
                tuning[key] = value
    explicit = {}
    for key in ('temperature','max_tokens'):
        if key in inputs:
            wire = 'max_output_tokens' if key == 'max_tokens' and protocol == 'responses' else key
            tuning[wire] = inputs[key]
            explicit[wire] = inputs[key]
    for field, value in native.items():
        if field in explicit and explicit[field] != value:
            raise ValueError(f'Public input conflicts with native {field}')
        tuning[field] = value
    parameters = merge_params(resolved, tuning, inputs.get('provider_params'))
    token_field = 'max_tokens' if protocol == 'anthropic' else 'max_output_tokens'
    tokens = parameters.get(token_field)
    if protocol == 'anthropic' or token_field in parameters:
        if not isinstance(tokens,int) or isinstance(tokens,bool) or tokens <= 0:
            raise ValueError(f'{token_field} must be a positive integer')
    return resolved, operation, {**parameters, **content, 'model':resolved.id, 'stream':stream}


def normalize_response(protocol, payload, request_id=None):
    if not isinstance(payload, dict):
        raise NewAPIError('invalid_response','LLM response must be an object',outcome_unknown=True)
    if protocol == 'anthropic':
        blocks = payload.get('content')
        finish = payload.get('stop_reason')
        if not isinstance(blocks,list) or not finish or any(not isinstance(block,dict) or not isinstance(block.get('type'),str) for block in blocks):
            raise NewAPIError('invalid_response','Messages response has invalid content or no stop reason',outcome_unknown=True)
        if any(block.get('type') == 'text' and not isinstance(block.get('text'),str) for block in blocks):
            raise NewAPIError('invalid_response','Messages text block is invalid',outcome_unknown=True)
        text = ''.join(block.get('text','') for block in blocks if block.get('type') == 'text')
        calls = [block for block in blocks if block.get('type') in {'tool_use','server_tool_use'}]
        refused = finish == 'refusal' or any(block.get('type') == 'refusal' for block in blocks)
        status = 'refused' if refused else 'completed' if finish in {'end_turn','tool_use','stop_sequence'} else 'incomplete'
        reason = finish if status != 'completed' else None
        result = {'content':blocks}
    else:
        blocks = payload.get('output')
        if not isinstance(blocks,list) or any(not isinstance(block,dict) or not isinstance(block.get('type'),str) for block in blocks):
            raise NewAPIError('invalid_response','Responses response has invalid output',outcome_unknown=True)
        for block in blocks:
            if block.get('type') == 'message':
                parts = block.get('content')
                if not isinstance(parts,list) or any(not isinstance(part,dict) or part.get('type') == 'output_text' and not isinstance(part.get('text'),str) for part in parts):
                    raise NewAPIError('invalid_response','Responses message content is invalid',outcome_unknown=True)
        text = ''.join(part.get('text','') for block in blocks if block.get('type') == 'message' for part in block.get('content',[]) if isinstance(part,dict) and part.get('type') == 'output_text')
        calls = [block for block in blocks if block.get('type') == 'function_call']
        refused = any(part.get('type') == 'refusal' for block in blocks if block.get('type') == 'message' for part in block.get('content',[]) if isinstance(part,dict))
        finish = payload.get('status')
        if finish not in {'completed','incomplete','failed','cancelled','canceled','queued','in_progress'}:
            raise NewAPIError('invalid_response','Responses response has no recognized status',outcome_unknown=True)
        status = 'refused' if refused else finish
        reason = payload.get('incomplete_details') or payload.get('error') or ('refusal' if refused else finish if finish != 'completed' else None)
        result = {'output':blocks}
    for call in calls:
        arguments = call.get('input') if protocol == 'anthropic' else call.get('arguments')
        try:
            decoded = json.loads(arguments) if isinstance(arguments,str) else arguments
        except ValueError:
            decoded = None
        if not isinstance(call.get('name'),str) or not isinstance(decoded,dict):
            raise NewAPIError('invalid_response','LLM tool call has invalid name or JSON arguments',outcome_unknown=True)
    if payload.get('usage') is not None and not isinstance(payload['usage'],dict):
        raise NewAPIError('invalid_response','LLM usage must be an object',outcome_unknown=True)
    if status == 'completed' and not text and not calls:
        status, reason = 'failed', 'no_usable_output'
    return {**result, **({'stream_events':payload['stream_events']} if 'stream_events' in payload else {}), 'text':text, 'tool_calls':calls, 'usage':payload.get('usage') or {}, 'finish_reason':finish, 'status':status, 'failure_reason':reason, 'model':payload.get('model'), 'protocol':protocol, 'request_id':request_id or payload.get('request_id'), 'id':payload.get('id')}


def _event_error(event, data):
    if not isinstance(data, dict):
        raise NewAPIError('invalid_stream','LLM stream event must be an object',outcome_unknown=True)
    if event == 'error' or data.get('type') == 'error' or data.get('error'):
        error = data.get('error') or {}
        error = error if isinstance(error,dict) else {}
        raise NewAPIError(error.get('code') or error.get('type') or 'stream_error',error.get('message') or data.get('message') or 'LLM stream failed')


def _index(data, field='index'):
    value = data.get(field)
    if not isinstance(value,int) or isinstance(value,bool) or value < 0:
        raise NewAPIError('invalid_stream',f'LLM stream has invalid {field}',outcome_unknown=True)
    return value


def aggregate_messages(frames):
    message, blocks, open_blocks, fragments = None, {}, set(), {}
    for event, data in frames:
        if data == '[DONE]':
            break
        _event_error(event,data)
        kind = data.get('type') or event
        if kind == 'ping':
            continue
        if kind == 'message_start':
            if message is not None or not isinstance(data.get('message'),dict):
                raise NewAPIError('invalid_stream','Invalid Messages stream start',outcome_unknown=True)
            message = deepcopy(data['message'])
            blocks = {i:deepcopy(block) for i,block in enumerate(message.get('content',[]))}
        elif message is None:
            raise NewAPIError('invalid_stream','Messages stream has no message_start',outcome_unknown=True)
        elif kind == 'content_block_start':
            index = _index(data)
            if index in blocks or not isinstance(data.get('content_block'),dict):
                raise NewAPIError('invalid_stream','Invalid Messages content block',outcome_unknown=True)
            blocks[index] = deepcopy(data['content_block'])
            open_blocks.add(index)
        elif kind == 'content_block_delta':
            index = _index(data)
            if index not in open_blocks or not isinstance(data.get('delta'),dict):
                raise NewAPIError('invalid_stream','Messages delta has no open block',outcome_unknown=True)
            delta, block = data['delta'], blocks[index]
            delta_type = delta.get('type')
            field = {'text_delta':'text','thinking_delta':'thinking','signature_delta':'signature'}.get(delta_type)
            if field:
                if not isinstance(delta.get(field),str):
                    raise NewAPIError('invalid_stream','Messages text delta is invalid',outcome_unknown=True)
                block[field] = block.get(field,'') + delta[field]
            elif delta_type == 'input_json_delta':
                fragments[index] = fragments.get(index,'') + delta['partial_json']
            elif delta_type == 'citations_delta':
                block.setdefault('citations',[]).append(deepcopy(delta['citation']))
            else:
                raise NewAPIError('unsupported_stream_delta','Unsupported Messages stream delta',outcome_unknown=True)
        elif kind == 'content_block_stop':
            index = _index(data)
            if index not in open_blocks:
                raise NewAPIError('invalid_stream','Messages block stop has no open block',outcome_unknown=True)
            if index in fragments:
                try:
                    blocks[index]['input'] = json.loads(fragments[index])
                except (ValueError,TypeError):
                    raise NewAPIError('invalid_stream','Messages tool arguments are invalid JSON',outcome_unknown=True) from None
            open_blocks.remove(index)
        elif kind == 'message_delta':
            message.update(data.get('delta') or {})
            message['usage'] = {**message.get('usage',{}), **(data.get('usage') or {})}
        elif kind == 'message_stop':
            if open_blocks:
                raise NewAPIError('incomplete_stream','Messages stream ended with an open content block',outcome_unknown=True)
            message['content'] = [blocks[index] for index in sorted(blocks)]
            return message
        else:
            raise NewAPIError('unsupported_stream_event','Unsupported Messages stream event',outcome_unknown=True)
    raise NewAPIError('incomplete_stream','Messages stream ended before message_stop; outcome unknown',outcome_unknown=True)


def _output_index(outputs, data):
    if 'output_index' in data:
        return _index(data,'output_index')
    found = [index for index,item in outputs.items() if item.get('id') == data.get('item_id') and data.get('item_id')]
    if len(found) == 1:
        return found[0]
    raise NewAPIError('invalid_stream','Responses event has no recognized output item',outcome_unknown=True)


def _response_part(outputs, data, field='content', index_field='content_index'):
    index = _output_index(outputs,data)
    if index not in outputs:
        raise NewAPIError('invalid_stream','Responses event references an unknown item',outcome_unknown=True)
    item = outputs[index]
    parts = item.setdefault(field,[])
    position = _index(data,index_field)
    if not isinstance(parts,list) or position > len(parts):
        raise NewAPIError('invalid_stream','Responses content index is out of sequence',outcome_unknown=True)
    return parts, position


def aggregate_responses(frames):
    response, outputs, extras = {}, {}, []
    terminals = {'response.completed','response.done','response.failed','response.incomplete','response.cancelled','response.canceled'}
    for event, data in frames:
        if data == '[DONE]':
            break
        _event_error(event,data)
        kind = data.get('type') or event
        if kind in {'response.created','response.in_progress'}:
            if not isinstance(data.get('response'),dict):
                raise NewAPIError('invalid_stream','Responses start has no response object',outcome_unknown=True)
            response.update(deepcopy(data['response']))
            if response.get('output'):
                outputs = {index:deepcopy(item) for index,item in enumerate(response['output'])}
        elif kind in {'response.output_item.added','response.output_item.done'}:
            index = _index(data,'output_index')
            if not isinstance(data.get('item'),dict):
                raise NewAPIError('invalid_stream','Responses event has no output item',outcome_unknown=True)
            outputs[index] = deepcopy(data['item'])
        elif kind in {'response.content_part.added','response.content_part.done','response.reasoning_summary_part.added','response.reasoning_summary_part.done'}:
            summary = kind.startswith('response.reasoning_summary')
            parts, index = _response_part(outputs,data,'summary' if summary else 'content','summary_index' if summary else 'content_index')
            if not isinstance(data.get('part'),dict):
                raise NewAPIError('invalid_stream','Responses event has no content part',outcome_unknown=True)
            if index == len(parts):
                parts.append(deepcopy(data['part']))
            else:
                parts[index] = deepcopy(data['part'])
        elif kind in {'response.output_text.delta','response.output_text.done','response.refusal.delta','response.refusal.done','response.reasoning_summary_text.delta','response.reasoning_summary_text.done'}:
            summary = kind.startswith('response.reasoning_summary')
            parts, index = _response_part(outputs,data,'summary' if summary else 'content','summary_index' if summary else 'content_index')
            if index == len(parts):
                raise NewAPIError('invalid_stream','Responses delta has no content part',outcome_unknown=True)
            field = 'refusal' if kind.startswith('response.refusal') else 'text'
            value = data.get('delta') if kind.endswith('.delta') else data.get(field)
            if not isinstance(value,str):
                raise NewAPIError('invalid_stream','Responses text delta is invalid',outcome_unknown=True)
            parts[index][field] = parts[index].get(field,'') + value if kind.endswith('.delta') else value
        elif kind in {'response.function_call_arguments.delta','response.function_call_arguments.done'}:
            index = _output_index(outputs,data)
            if index not in outputs or outputs[index].get('type') != 'function_call':
                raise NewAPIError('invalid_stream','Responses arguments have no function call',outcome_unknown=True)
            value = data.get('delta') if kind.endswith('.delta') else data.get('arguments')
            if kind.endswith('.delta'):
                if not isinstance(value,str) or not isinstance(outputs[index].get('arguments',''),str):
                    raise NewAPIError('invalid_stream','Responses function arguments delta is invalid',outcome_unknown=True)
                outputs[index]['arguments'] = outputs[index].get('arguments','') + value
            else:
                outputs[index]['arguments'] = deepcopy(value)
        elif kind == 'response.output_text.annotation.added':
            parts, index = _response_part(outputs,data)
            if index == len(parts):
                raise NewAPIError('invalid_stream','Responses annotation has no content part',outcome_unknown=True)
            parts[index].setdefault('annotations',[]).append(deepcopy(data.get('annotation')))
        elif kind in terminals:
            terminal = data.get('response')
            if not isinstance(terminal,dict) or terminal.get('status') not in {'completed','failed','incomplete','cancelled','canceled'}:
                raise NewAPIError('invalid_stream','Responses terminal event has no terminal status',outcome_unknown=True)
            expected = {'response.completed':{'completed'},'response.failed':{'failed'},'response.incomplete':{'incomplete'},'response.cancelled':{'cancelled','canceled'},'response.canceled':{'cancelled','canceled'}}
            if kind in expected and terminal['status'] not in expected[kind]:
                raise NewAPIError('invalid_stream','Responses completion conflicts with terminal status',outcome_unknown=True)
            response.update(deepcopy(terminal))
            if 'output' not in terminal:
                response['output'] = [outputs[index] for index in sorted(outputs)]
            if extras:
                response['stream_events'] = extras
            return response
        else:
            # Native service-tool events have no universal text mapping; retain them.
            extras.append(deepcopy(data))
    raise NewAPIError('incomplete_stream','Responses stream ended before a terminal response; outcome unknown',outcome_unknown=True)
