"""Best-effort normalization of agent CLI JSON events. Provider payloads vary by version.
Never fabricate substep allocation or costs; attach usage to the supplied node only.
"""
from decimal import Decimal

def normalize(provider, payload, session, index):
    out=[]
    if provider=='codex':
        if payload.get('type')=='turn.completed':
            usage=payload.get('usage') or payload.get('turn',{}).get('usage') or {}
            if usage:
                out.append({'type':'usage','usage':{'session_id':session,'usage_key':f'turn:{index}',
                    'input_tokens':usage.get('input_tokens',0),'output_tokens':usage.get('output_tokens',0),
                    'cached_input_tokens':usage.get('cached_input_tokens',0),'reasoning_tokens':usage.get('reasoning_output_tokens',0),
                    'cost_source':'unavailable'}})
    elif provider=='claude':
        if payload.get('type')=='result':
            usage=payload.get('usage') or {}
            cost=payload.get('total_cost_usd')
            if isinstance(cost,(int,float,str)) and Decimal(str(cost))>=0:
                cost=str(cost)
            else: cost=None
            out.append({'type':'usage','usage':{'session_id':session,'usage_key':'session:total',
                'input_tokens':usage.get('input_tokens',0),'output_tokens':usage.get('output_tokens',0),
                'cached_input_tokens':usage.get('cache_read_input_tokens',0),
                'cost_usd':cost,'cost_source':'reported' if cost is not None else 'unavailable'}})
    return out
