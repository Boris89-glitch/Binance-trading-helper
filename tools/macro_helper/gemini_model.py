"""Free-tier inference via the Google Gemini API (AI Studio key, no billing required)."""
import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_MODEL='gemini-3.8-flash'
# Tried in order when the preferred model is overloaded (503), out of free quota (429) or times out.
FALLBACK_MODELS=('gemini-2.5-flash','gemini-3.5-flash')
ENDPOINT='https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent'


def valid_key(key):
    return isinstance(key,str) and 20<=len(key.strip())<=200 and not any(c.isspace() for c in key.strip())


def infer(context,config):
    from .sentiment import SYSTEM,PAIR_SYSTEM,schema,validate_output,validate_pair_output
    pairs=context.get('kind')=='pairs'
    shape=schema([p['symbol'] for p in context['pairs']] if pairs else None)
    instructions=(PAIR_SYSTEM if pairs else SYSTEM)+'\nReturn only one JSON object matching this JSON Schema:\n'+json.dumps(shape)
    # Thinking tokens count toward maxOutputTokens, so caps are higher than Anthropic's.
    payload={'systemInstruction':{'parts':[{'text':instructions}]},
             'contents':[{'role':'user','parts':[{'text':'Produce the monthly verdict from this evidence JSON:\n'+json.dumps(context,ensure_ascii=False)}]}],
             'generationConfig':{'temperature':0.2,'maxOutputTokens':32768 if pairs else 8192,'responseMimeType':'application/json'}}
    errors=[]
    for model in dict.fromkeys((config['model'],*FALLBACK_MODELS)):
        try:
            result=call(model,payload,config['api_key'])
            break
        except HTTPError as exc:
            # Do not include provider bodies/headers or secrets in browser or logs.
            if exc.code in (429,500,503,504):
                errors.append(f'{model} HTTP {exc.code}');continue
            reason={400:'Gemini rejected the request (check the API key)',403:'Gemini API key was denied',404:'The configured Gemini model is unavailable'}.get(exc.code,'Gemini request failed')
            raise ValueError(f'{reason} (HTTP {exc.code}); no analysis was archived') from None
        except (URLError,TimeoutError):
            errors.append(f'{model} timed out / unreachable');continue
    else:
        raise ValueError('Gemini is overloaded or the free-tier limit is reached ('+'; '.join(errors)+'); wait and try again. No analysis was archived') from None
    candidates=result.get('candidates') or []
    if not candidates:
        raise ValueError('Gemini returned no answer (the request may have been blocked); no analysis was archived')
    if candidates[0].get('finishReason')=='MAX_TOKENS':
        raise ValueError('The AI response was truncated; no analysis was archived')
    text=''.join(part.get('text','') for part in candidates[0].get('content',{}).get('parts',[]) if not part.get('thought'))
    parsed=validate_pair_output(text,context['pairs'],context.get('source_ledger',[]),context.get('recent_news')) if pairs else validate_output(text,context.get('source_ledger',[]))
    meta=result.get('usageMetadata',{})
    usage={'input_tokens':meta.get('promptTokenCount',0),'output_tokens':meta.get('candidatesTokenCount',0)+meta.get('thoughtsTokenCount',0),'billing_mode':'gemini_free_tier'}
    return parsed,usage,result.get('modelVersion',model)


def call(model,payload,api_key):
    if model.startswith('gemini-3'):
        # Low thinking keeps large evidence prompts well inside the timeout.
        payload={**payload,'generationConfig':{**payload['generationConfig'],'thinkingConfig':{'thinkingLevel':'low'}}}
    request=Request(ENDPOINT.format(quote(model,safe='.-')),data=json.dumps(payload).encode(),method='POST',headers={
        'Content-Type':'application/json','x-goog-api-key':api_key})
    with urlopen(request,timeout=120) as response:
        return json.loads(response.read(2_000_000))
