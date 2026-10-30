"""Versioned primary-source underwriting. Free calculations, one sealed review.

Numbers below are authored conservative policy sensitivities, not consensus or
reported forecasts. Raw originals and rejected reviews remain immutable.
"""
import copy
from datetime import timedelta
from decimal import Decimal
from .common import stable_id, parse_time, decimal_value
from .market import _primary_verified

CONTRACT='primary-underwriting-v1'
SYMBOLS=('AMZN','QQQ','SOXX')
POLICY='2026-10-30-conservative-operating-and-portfolio-v1'


def projection(raw):
    """Exclude unverified broker forecast/history blobs from the paid payload."""
    out={k:copy.deepcopy(raw[k]) for k in ('symbol','identity','primary_financials','primary_evidence','etf_valuation') if k in raw}
    out['company_profile']={k:v for k,v in (raw.get('company_profile') or {}).items() if k in ('name','company_name')}
    return out


def material_key(symbol,raw):
    primary=raw.get('primary_financials') or {}
    if symbol=='AMZN':
        material={k:primary.get(k) for k in ('operating_basis','latest_earnings_release','previous_earnings_release')}
    else:
        etf=raw.get('etf_valuation') or {}
        # Daily NAV/weight churn is an execution risk check, not a new valuation
        # model. A new monthly/daily valuation baseline or split is material.
        material={k:etf.get(k) for k in ('nav','pe','expense_ratio','corporate_actions','timing_contract')}
        for key in ('nav','pe','expense_ratio'):
            material[key]={k:v for k,v in (material[key] or {}).items() if k in ('value','as_of','currency','price_unit','unit')}
    return stable_id(CONTRACT,POLICY,symbol,material)


def author(symbol,raw,store,now):
    from .research import _calculate_target_prices, _etf_input_gaps, _nested_refs
    refs=sorted(_nested_refs(raw));primary=raw.get('primary_financials') or {}
    if not _primary_verified(store,primary.get('instrument') or {}):raise ValueError('PRIMARY_IDENTITY_REQUIRED')
    plan={'symbol':symbol,'currency':'USD','price_unit':'per_share','is_adr':False,
        'horizon_months':24,'conditions_parsed':True,'evidence_refs':refs,
        'time_exit_policy':'Every new issuer earnings/valuation baseline triggers fresh underwriting and independent review; review at next_review_at or earlier on thesis invalidation. At 24 months, exit/reassess manually; renewal requires a new accepted thesis.',
        'valuation_basis':{'kind':'independent_valuation','underwriting_contract':CONTRACT,'policy_version':POLICY,
            'forecast_kind':'authored_sensitivity_not_consensus','no_automatic_real_orders':True,
            'research_is_not_current_buy_signal':True},'targets':[]}
    scenarios=[]
    if symbol=='AMZN':
        basis=primary.get('operating_basis') or {}
        if set(basis)!={'operating_income_ttm','operating_cash_flow_ttm','free_cash_flow_ttm','diluted_shares_quarter'}:
            raise ValueError('AMZN_OPERATING_BASIS_REQUIRED')
        if not all(_primary_verified(store,v) for v in basis.values()):raise ValueError('AMZN_BASIS_SOURCE_INVALID')
        income=decimal_value(basis['operating_income_ttm']['value']);shares=decimal_value(basis['diluted_shares_quarter']['value'])
        day=basis['operating_income_ttm']['as_of']
        if (parse_time(now).date()-parse_time(day+'T00:00:00Z').date()).days>130:raise ValueError('AMZN_EARNINGS_STALE')
        if not primary.get('previous_earnings_release'):raise ValueError('AMZN_PRIOR_RELEASE_REQUIRED')
        plan['valuation_basis'].update(reported_operating_basis=basis,
            normalization='Use TTM OPERATING income, exclude ALL non-operating investment marks (including Anthropic) instead of annualizing GAAP EPS. Subtract an authored after-tax financing-cost proxy, tax explicitly, and dilute current quarterly weighted-average shares for two forward years. Operating EPS is an estimate, not reported/adjusted company EPS.',
            risk='Negative TTM free cash flow makes cash conversion and AI capital intensity the central risk. This operating-earnings model does not capitalize AI capex as immediate free cash flow and is not a DCF.',
            share_denominator='Current quarterly weighted-average diluted shares, not common shares outstanding; multiply by (1+future dilution)^2, avoiding a second addition of already diluted awards.')
        plan['thesis']='AMZN：AWS 与零售盈利增长可支持长期研究，但 AI 投资导致自由现金流承压。用已披露 TTM 经营利润估值，剔除投资重估收益；只在保守基准价值的七至八折区间考虑买入。增长、税率、净融资成本和未来稀释均为独立情景假设，不是公司指引或市场共识。'
        for name,growth,tax,cost,dilution,multiple in [('bear','-.10','.30','3','.02','18'),('base','.10','.25','2','.01','25'),('bull','.15','.23','1','.005','30')]:
            rationale=f'Authored 24-month {name} sensitivity: operating-profit CAGR {growth}, tax {tax}, after-tax financing cost USD {cost}bn, annual future dilution {dilution}, exit operating-EPS multiple {multiple}. Base 10% growth deliberately discounts historical growth for capital intensity; bear models two years of contraction, higher tax/cost/dilution; bull requires faster AWS/retail growth and improved cash conversion. Multiples are risk-policy choices, not issuer fair values or broker consensus.'
            assumptions={'operating_eps_bridge':{'operating_income':str(income),'annual_growth':growth,'years':'2','tax_rate':tax,
                'after_tax_nonoperating_cost':cost,'diluted_shares':str(shares*(1+decimal_value(dilution))**2)},
                'multiple':multiple,'currency':'USD','price_unit':'per_share','forecast_period':'24 months after '+day,
                'rationale':rationale,'evidence_refs':refs,'dilution_assumption':dilution}
            scenarios.append({'scenario':name,'price':'1','method':'eps_multiple','horizon_months':24,'assumptions':assumptions,'evidence_refs':refs})
        discounts=(Decimal('.70'),Decimal('.80'))
        plan['invalidation_conditions']=['Latest reported TTM operating profit falls more than 15% below the disclosed valuation anchor; freeze additions and reassess the entire thesis.',
            'A new earnings release supersedes the anchor, or dilution/financing/cash-conversion assumptions are materially contradicted; no further entry before fresh independent underwriting.',
            'Two consecutive new quarterly releases fail to improve negative TTM free cash flow while capital intensity rises; reassess growth and multiple rather than averaging down.']
    else:
        gaps=_etf_input_gaps(raw,now,set(refs),store)
        if gaps:raise ValueError('ETF_PRIMARY_INPUTS_REQUIRED:'+','.join(g['code'] for g in gaps))
        etf=raw['etf_valuation'];nav=etf['nav'];pe=etf['pe']
        from .research import _declared_effective_day
        for action in etf.get('corporate_actions',[]):
            effective=_declared_effective_day(action)
            if effective and str(effective)<=str(parse_time(now).date()) and nav['as_of']<str(effective):
                raise ValueError('POST_SPLIT_ISSUER_BASELINE_REQUIRED')
        settings=[('bear','-.05','20'),('base','.08','25'),('bull','.12','30')] if symbol=='QQQ' else [('bear','-.10','25'),('base','.10','35'),('bull','.15','45')]
        plan['thesis']=('QQQ：广泛持有纳斯达克大型非金融公司，科技与大盘成长集中度仍高。以同一天的官方净值与组合市盈率构造盈利代理，用增长与估值收缩的三种情景计算，不把 ETF 当成一家公司的 EPS。'
            if symbol=='QQQ' else 'SOXX：半导体受 AI 资本开支推动，同时具有高估值、行业周期与持仓集中风险。即使盈利增长，估值收缩仍可能压低价格；因此只在更深的安全边际内考虑入场。拆股生效后必须以发行人新单位净值重算并复核。')
        fee=etf['expense_ratio']['value']
        for name,growth,exitpe in settings:
            assumptions={'nav_per_share':nav['value'],'current_pe':pe['value'],'annual_earnings_growth':growth,
                'exit_pe':exitpe,'years':'2','currency':'USD','price_unit':'per_share',
                'fees_treatment':f'Authored growth is NET of annual fund expense ratio {fee}%; do not subtract it twice. Price-only scenario excludes cash distributions; not a total-return forecast.',
                'rationale':f'Authored {name} 24-month sensitivity, earnings-proxy CAGR {growth}, exit portfolio P/E {exitpe}. Base growth is conservative policy, not consensus; the lower base exit multiple explicitly models valuation compression. Bear contracts earnings; bull requires continuing AI/platform demand and no severe multiple compression. Portfolio P/E methodology can exclude loss-makers; the NAV/P/E proxy is not reported corporate EPS. Current holdings/concentration are separately dated risk evidence.',
                'evidence_refs':refs,'forecast_period':'24 months after '+nav['as_of']}
            scenarios.append({'scenario':name,'price':'1','method':'etf_nav_scenario','horizon_months':24,'assumptions':assumptions,'evidence_refs':refs})
        discounts=(Decimal('.75'),Decimal('.80')) if symbol=='QQQ' else (Decimal('.65'),Decimal('.75'))
        plan['valuation_basis'].update(anchor_date=nav['as_of'],nav=nav['value'],portfolio_pe=pe['value'],
            timing_disclosure=etf.get('timing_disclosure','Issuer NAV/PE/holdings share one valuation date.'),
            holdings_as_of=etf['holdings']['as_of'],top10_weight=etf['holdings']['concentration']['top10_weight'],
            annual_expense_percent=fee,execution_nav_max_age_days=7,maximum_entry_nav_premium='0.005',
            split_policy='Expire at the last regular XNYS close before the announced split day. Never mechanically adjust portfolio/ledger quantities or reuse pre-split entry prices. Require post-split issuer NAV/PE and independent review.',
            model_limit='NAV divided by weighted portfolio P/E is a valuation proxy; composition changes, loss-maker exclusions and multiple compression can invalidate it. No distribution reinvestment assumed.')
        plan['invalidation_conditions']=['Issuer changes the fund objective/index, fees, valuation methodology or share units; suspend entries pending independent review.',
            'A new issuer valuation baseline supersedes the model, or official NAV/holdings are stale; block entry and refresh sources.',
            'Material portfolio concentration or semiconductor/platform earnings deterioration contradicts the chosen growth sensitivity; revise the model before any additional purchase.']
    plan['targets']=scenarios;plan,_=_calculate_target_prices(plan)
    target={r['scenario']:decimal_value(r['price']) for r in plan['targets']}
    cents=lambda v:str(v.quantize(Decimal('.01')))
    low,high=[cents(target['base']*d) for d in discounts]
    plan['entry']={'low':low,'high':high,'amount_usd':'1000','basis':'independent_valuation','source_level_ids':[],
        'conditions':[f'New-entry price must be between USD {low} and {high} per share, inclusive.'],
        'condition_mode':'price_only','execution_conditions':[{'type':'price_between','low':low,'high':high}]}
    plan['stop']={'price':cents(decimal_value(low)*Decimal('.90')),'confirmation':'daily_close'}
    plan['take_profit']=[{'price':cents(target['base']),'fraction':'.5'},{'price':cents(target['bull']),'fraction':'.5'}]
    plan['valuation_basis'].update(entry_policy=f'Authored safety discount: {discounts[0]}–{discounts[1]} times base scenario; not a creator entry zone.',
        risk_policy='Initial stop is 10% below entry.low, confirmed by regular daily close. Gap losses can exceed this risk budget. Bear fair value is a downside sensitivity, never a profit target.',
        new_entry_scope='USD 1000 proposed initial tranche; existing holdings need their own reconciled cost basis. Fresh regular-session official quote, cash, fees and all existing risk fences remain mandatory.')
    return plan


def advance(store,model,fundamentals,now):
    from .research import compile_plan, plan_fingerprint, _unresolved_thesis_defer
    result={'processed':0,'errors':[],'deferred':[]}
    for symbol in SYMBOLS:
        previous=store.get('plan',stable_id('investment-plan',symbol))
        if not previous:continue
        discovered=store.get('research_discovered_symbol',symbol) or {}
        source=store.get('research_evidence',discovered.get('source_id','')) or store.get('research_evidence',previous.get('research_snapshot_id',''))
        raw=projection(fundamentals.get(symbol) or {})
        key=material_key(symbol,raw);state=store.get('primary_underwriting',key)
        if state and state.get('terminal'):
            if source:
                link=stable_id(CONTRACT,'source-link',symbol,source['id'],key)
                store.put('primary_underwriting_source',link,{'symbol':symbol,'source_id':source['id'],
                    'video_id':source.get('video_id'),'valuation_key':key,'plan_revision':state.get('revision'),
                    'status':'reused_unchanged_primary_valuation','creator_report_is_separately_sealed':True,
                    'valuation_is_independent_of_creator_technical_levels':True},immutable=True)
            continue
        if not source:continue
        if _unresolved_thesis_defer(store,previous,source,{symbol:raw},now):
            result['deferred'].append({'symbol':symbol,'error':'UNRESOLVED_VERIFIED_THESIS_RISK'});continue
        if any(not store.get('model_receipt',k) and not store.get('model_rejection',k) for k,_ in store.items('model_call')):
            result['deferred'].append({'symbol':symbol,'error':'UnknownModelOutcome'});break
        try:
            frozen=store.get('primary_underwriting_input',key)
            if not frozen:
                candidate=author(symbol,raw,store,now)
                context={'kind':CONTRACT,'event_id':key,'system_owned':True,'review_only_candidate':candidate,
                    'candidate_hash':plan_fingerprint(candidate),'policy_version':POLICY}
                frozen={'context':context,'fundamentals':raw,'source_id':source['id'],'as_of':now}
                store.put('primary_underwriting_input',key,frozen,immutable=True)
            source=store.get('research_evidence',frozen['source_id'])
            levels=[v for _,v in store.items('research_level') if v.get('symbol')==symbol and source['id'] in v.get('evidence_refs',[])]
            plans=compile_plan(store,source,levels,model,{symbol:frozen['fundamentals']},frozen['as_of'],context=frozen['context'])
            chosen=next((p for p in plans if p.get('symbol')==symbol),None)
            if not chosen:raise ValueError('UNDERWRITING_NOT_COMMITTED')
            store.put('primary_underwriting',key,{'terminal':True,'symbol':symbol,'status':chosen.get('status'),
                'revision':chosen['revision'],'review_issues':chosen.get('review_issues',[]),'timestamp':now,'policy':POLICY})
            result['processed']+=1
        except Exception as exc:
            code=type(exc).__name__
            retry=code in ('ModelBudgetExceeded','UnknownModelOutcome','TimeoutError','ConnectionError')
            store.put('primary_underwriting',key,{'terminal':False,'symbol':symbol,'status':'budget_deferred' if code=='ModelBudgetExceeded' else 'source_or_review_error',
                'error':code,'detail':str(exc)[:400] if isinstance(exc,(ValueError,KeyError)) else code,'timestamp':now,
                'next_source_check':(parse_time(now)+timedelta(hours=6)).isoformat()})
            result['deferred' if retry else 'errors'].append({'symbol':symbol,'error':code})
    return result


def execution_gaps(store,plan,snapshot,now):
    basis=plan.get('valuation_basis') or {}
    if basis.get('underwriting_contract')!=CONTRACT:return []
    raw=((store.get('fundamental_cache',plan['symbol']) or {}).get('data') or {})
    key=material_key(plan['symbol'],projection(raw));state=store.get('primary_underwriting',key) or {}
    if state.get('status')!='active' or state.get('revision')!=plan.get('revision'):
        return ['primary_valuation_changed_requires_review']
    if plan['symbol']=='AMZN':return []
    etf=raw.get('etf_valuation') or {};nav=etf.get('latest_nav') or etf.get('nav') or {}
    try:
        current_top=decimal_value(etf['holdings']['concentration']['top10_weight'])
        if current_top>decimal_value(basis['top10_weight'])+Decimal('.05'):
            return ['portfolio_concentration_changed_requires_review']
    except (ValueError,KeyError,TypeError):return ['current_issuer_holdings_required']
    try:
        if (not _primary_verified(store,nav) or (parse_time(now).date()-parse_time(nav['as_of']+'T00:00:00Z').date()).days not in range(8)
            or decimal_value(nav['value'])<=0):return ['current_issuer_nav_required']
        quote=snapshot.get('selected',snapshot)
        if decimal_value(quote['price'])>decimal_value(nav['value'])*(1+decimal_value(basis['maximum_entry_nav_premium'])):
            return ['etf_nav_premium_exceeds_policy']
    except (ValueError,KeyError,TypeError):return ['current_issuer_nav_required']
    return []
