"""Reproducible one-article/one-bar/one-event/one-opinion OFFLINE fixture chain."""
import csv
import json
import sys
from pathlib import Path

from cheetah_mir.core import Workspace
from cheetah_mir.adapters import ingest_feed, build_graph
from cheetah_mir.cli import export, status

AT = '2026-11-05T10:00:00+08:00'
PUB = '2026-11-04T08:00:00+08:00'
QUOTE = 'If market breadth improves for three sessions, the rebound may persist for five trading days.'

def run(root):
    w = Workspace(root, initialize=True)
    fixtures = Path(__file__).resolve().parents[1] / 'tests/fixtures'
    source = w.source('MOCK publisher', 'https://example.invalid', 'B', collected_at=AT, rights='CC0 synthetic fixture')
    article = w.ingest_file(fixtures/'article.md', source, collected_at=AT, published_at=PUB)
    trader = w.entity('MOCK Trader A', 'PERSON', collected_at=AT, document_id=article)
    quote = w.evidence(article, QUOTE, 'paragraph 1', collected_at=AT)
    opinion = w.opinion(trader, quote, opinion_state='FORECAST', target='MOCK market', horizon='five sessions conditional on three-session breadth improvement', collected_at=AT, published_at=PUB)
    event = w.put('event', 'MOCK-EVT-01', {'name': 'MOCK rebound observation', 'event_type': 'MARKET',
        'event_at': '2026-11-04T15:00:00+08:00', 'sample_kind': 'MOCK', 'fact_status': 'UNVERIFIED'},
        collected_at=AT, published_at=PUB, source_id=source, document_id=article)
    raw_market = w.ingest_file(fixtures/'market.csv', source, collected_at=AT)
    asset = w.asset('MOCK_INDEX', 'MOCK', 'INDEX', collected_at=AT, document_id=raw_market)
    with (fixtures/'market.csv').open(encoding='utf-8', newline='') as f:
        bar = w.market_bar(asset, next(csv.DictReader(f)), document_id=raw_market, collected_at=AT)
    observation = w.put('observation', [event, bar], {'observation_type': 'MARKET_RESPONSE', 'asset_id': asset,
        'market_bar_id': bar, 'event_id': event, 'sample_kind': 'MOCK', 'conclusion': None, 'fact_status': 'UNVERIFIED'},
        collected_at=AT, source_id=source, document_id=raw_market)
    for left, right, relation in ((trader, article, 'PUBLISHED'), (trader, opinion, 'EXPRESSED'),
                                 (opinion, event, 'REFERENCES'), (observation, event, 'OCCURRED_AFTER')):
        w.relation(left, right, relation, quote, collected_at=AT, valid_from=PUB)
    case = w.put('case', 'RQ-OBS-01-MOCK-01', {'research_question_id': 'RQ-OBS-01', 'event_id': event, 'opinion_id': opinion,
        'market_bar_id': bar, 'observation_id': observation, 'sample_kind': 'MOCK', 'research_state': 'REGISTERED'},
        collected_at=AT, source_id=source, document_id=article)
    hypothesis = w.put('hypothesis', 'H-OBS-MOCK-01', {'statement': 'MOCK breadth improvement hypothesis; not a finding',
        'case_id': case, 'research_state': 'REGISTERED', 'evaluation_plan': 'pre-register universe, benchmark, windows and failure rules before outcomes'}, collected_at=AT)
    ingest_feed(w, fixtures/'feed.xml', source, collected_at=AT)
    graph = build_graph(w)
    result = {'sample_kind': 'MOCK', 'verified_real_market_samples': 0, 'article': article, 'bar': bar,
              'event': event, 'opinion': opinion, 'case': case, 'hypothesis': hypothesis,
              'graph_nodes': graph.number_of_nodes(), 'graph_edges': graph.number_of_edges(), 'status': status(w)}
    export(w)
    w.safe_path('reports/demo_chain.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
    return result

if __name__ == '__main__':
    print(json.dumps(run(Path(sys.argv[1])), ensure_ascii=False, indent=2, sort_keys=True))
