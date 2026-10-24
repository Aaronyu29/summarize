"""Explicit independent price-only review fixtures, shared by lifecycle tests."""
import copy


def with_price_conditions(plan):
    entry = plan['entry']
    entry.setdefault('conditions', ['Price is inside the independently reviewed entry interval'])
    entry.update(condition_mode='price_only', execution_conditions=[{
        'type': 'price_between', 'low': entry['low'], 'high': entry['high']}])
    plan['entry_condition_review'] = {
        'condition_mode': 'price_only', 'all_text_conditions_covered': True,
        'conditions': copy.deepcopy(entry['conditions']),
        'execution_conditions': copy.deepcopy(entry['execution_conditions'])}
    plan['conditions_parsed'] = True
    return plan


def seal_fixture_review(store, plan, review, evidence_ids=None):
    """Supply an explicit independent result for these known price-only fixtures.

    Production seal_plan_review deliberately never fabricates this result from
    author fields. Negative/legacy tests call that production function directly.
    """
    from rhino_invest.research import seal_plan_review
    reviewed = copy.deepcopy(review)
    if plan.get('entry_condition_review') is not None:
        reviewed.setdefault('entry_condition_review', copy.deepcopy(plan['entry_condition_review']))
    return seal_plan_review(store, plan, reviewed, evidence_ids)
