"""Text/tool-call gateway capability; does not replace the external agent model."""
from pathlib import Path
import tempfile

from tools.base_tool import BaseTool, ToolTier, ToolRuntime, ToolStability, ToolStatus, Determinism, RetryPolicy, ResourceProfile, ToolResult
from tools.provider_pricing import PriceQuoteRequired
from tools._newapi.config import load_settings
from tools._newapi.client import NewAPIClient, NewAPIError, redact, sanitize_error, read_sse
from tools._newapi.models import model_catalog
from tools._newapi.llm import build_request, normalize_response, aggregate_messages, aggregate_responses


class NewAPILLM(BaseTool):
    name = 'newapi_llm'
    provider = 'newapi'
    capability = 'text_generation'
    tier = ToolTier.GENERATE
    runtime = ToolRuntime.API
    stability = ToolStability.BETA
    determinism = Determinism.STOCHASTIC
    dependencies = ['env:NEW_API_KEY']
    resource_profile = ResourceProfile(network_required=True)
    retry_policy = RetryPolicy(max_retries=0)
    side_effects = ['paid gateway request', 'optional text file']
    install_instructions = 'Administrator: configure newapi base URL and text model profiles in config.yaml. User: set NEW_API_KEY.'
    best_for = ['explicit stage text/structured output', 'returning native tool calls to the agent']
    not_good_for = ['hosting the agent conversation', 'background or WebSocket LLM tasks']
    input_schema = {'type':'object','properties':{
        'model':{'type':'string'},'protocol':{'type':'string','enum':['auto','anthropic','responses'],'default':'auto'},
        'system':{'type':'string'},'messages':{'type':'array'},'request':{'type':'object'},
        'temperature':{'type':'number'},'max_tokens':{'type':'integer','minimum':1},
        'stream':{'type':'boolean','default':False},'provider_params':{'type':'object'},'output_path':{'type':'string'}
    }}
    idempotency_key_fields = ['model','protocol','system','messages','request','temperature','max_tokens','stream','provider_params']

    def __init__(self, config_path=None):
        self.config_path = config_path

    def get_status(self):
        try:
            settings = load_settings(self.config_path)
            return ToolStatus.AVAILABLE if settings.configured and model_catalog(settings,self.capability) else ToolStatus.UNAVAILABLE
        except Exception:
            return ToolStatus.UNAVAILABLE

    def get_info(self):
        info = super().get_info()
        info['hosting_provider'] = 'newapi'
        try:
            settings = load_settings(self.config_path)
            info['model_catalog'] = model_catalog(settings,self.capability)
            info['capabilities'] = sorted({operation for profile in info['model_catalog'].values() for protocol,operation in [('anthropic','messages'),('responses','responses')] if protocol in profile['protocols'] and operation in profile['operations']})
            info['supports'] = {'stream':True,'native_request':True,'background':False,'websocket':False}
        except Exception as exc:
            info['model_catalog'] = {}
            info['config_error'] = str(sanitize_error(None,exc))
        return info

    def estimate_cost(self, inputs):
        raise PriceQuoteRequired('New API model price requires a deployment/account quote')

    def execute(self, inputs):
        settings, model = None, None
        try:
            settings = load_settings(self.config_path)
            resolved, operation, payload = build_request(settings, inputs)
            model = resolved.id
            client = NewAPIClient(settings)
            headers = {'anthropic-version':'2023-06-01'} if resolved.protocol == 'anthropic' else None
            response = client.request('POST','/v1/'+operation,json=payload,headers=headers,stream=payload['stream'])
            try:
                try:
                    decoded = (aggregate_messages if resolved.protocol == 'anthropic' else aggregate_responses)(read_sse(response,deadline=response._newapi_deadline)) if payload['stream'] else response.json()
                    result = normalize_response(resolved.protocol,decoded,response.headers.get('x-request-id'))
                except (ValueError,TypeError,KeyError) as exc:
                    if isinstance(exc,NewAPIError):
                        raise
                    raise NewAPIError('invalid_response','LLM returned malformed JSON or stream data; outcome unknown',outcome_unknown=True) from None
            finally:
                response.close()
            result.update({'provider':'newapi','hosting_provider':'newapi','model':model,'operation':operation,'cost_status':'unquoted'})
            result = redact(result,settings.api_key,drop_credentials=False)
            artifacts = []
            if result['status'] == 'completed' and result['text'] and inputs.get('output_path'):
                path = Path(inputs['output_path'])
                path.parent.mkdir(parents=True,exist_ok=True)
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=path.parent,prefix=path.name+'.',suffix='.part',mode='w',encoding='utf8',delete=False) as file:
                        temporary = Path(file.name)
                        file.write(result['text'])
                    temporary.replace(path)
                    artifacts.append(str(path))
                    result['output_path'] = str(path)
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            success = result['status'] == 'completed'
            error = None
            if not success:
                error = sanitize_error(settings,NewAPIError('llm_'+result['status'],'LLM response '+result['status']+': '+str(result['failure_reason']),status=response.status_code,request_id=result.get('request_id')))
                result['error'] = error.as_data()
                result['failure_reason'] = str(error)
            return ToolResult(success=success,data=result,error=str(error) if error else None,cost_usd=None,model=model,artifacts=artifacts)
        except Exception as exc:
            error = sanitize_error(settings,exc)
            return ToolResult(success=False,error=str(error),data={'provider':'newapi','hosting_provider':'newapi','cost_status':'unquoted','error':error.as_data()},cost_usd=None,model=model)
