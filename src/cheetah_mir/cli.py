from __future__ import annotations
import argparse
import csv
import json
import sys
from pathlib import Path
from .core import Workspace, canonical
from .adapters import ingest_feed, ingest_pdf, build_graph, RemoteAdapter

def status(workspace):
    from collections import Counter
    with workspace.db() as c:
        failures = c.execute('SELECT count(*) FROM failures').fetchone()[0]
        conflicts = c.execute('SELECT count(*) FROM conflicts').fetchone()[0]
    return {'counts': dict(sorted(Counter(r['kind'] for r in workspace.list()).items())), 'failures': failures,
            'conflicts': conflicts, 'network_access': 'DISABLED', 'live_ingestion': 'DISABLED',
            'auto_trading': 'DISABLED', 'provenance': workspace.verify()}

def export(workspace, *, as_of=None):
    records = workspace.list(as_of=as_of)
    output = workspace.safe_path('reports/export.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(records, ensure_ascii=False, sort_keys=True, indent=2), encoding='utf-8')
    with workspace.safe_path('reports/export.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f); writer.writerow(['id', 'kind', 'source_id', 'document_id', 'available_at', 'content_hash'])
        writer.writerows([r[k] for k in ('id', 'kind', 'source_id', 'document_id', 'available_at', 'content_hash')] for r in records)
    lines = ['# MIR research inventory', '', 'Network / live ingestion / trading: DISABLED', '',
             '| Record | Type | Evidence document | Available at (UTC) |', '|---|---|---|---|']
    lines += [f"| {r['id']} | {r['kind']} | {r['document_id'] or '-'} | {r['available_at']} |" for r in records]
    workspace.safe_path('reports/status.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return {'records': len(records), 'output': str(output)}

def main(argv=None):
    p = argparse.ArgumentParser(prog='mir', description='Offline, source-linked research records')
    p.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
    subs = p.add_subparsers(dest='command', required=True)
    subs.add_parser('init'); subs.add_parser('audit')
    sources = subs.add_parser('sources'); sources.add_argument('action', choices=['list', 'validate', 'add']); sources.add_argument('--json', type=Path)
    for name in ('ingest', 'import'):
        ingest = subs.add_parser(name)
        ingest.add_argument('kind', choices=['file', 'rss', 'market', 'trader-document', 'market-data', 'pdf'])
        ingest.add_argument('--file', type=Path, required=True); ingest.add_argument('--source-id', required=True)
        ingest.add_argument('--collected-at', required=True); ingest.add_argument('--published-at'); ingest.add_argument('--asset-id')
    for name in ('events', 'entities'):
        sub = subs.add_parser(name); sub.add_argument('action', choices=['list'])
    record = subs.add_parser('record'); record.add_argument('--json', type=Path, required=True)
    for name, actions in [('graph', ['build', 'inspect']), ('data', ['validate']), ('provenance', ['verify']), ('report', ['status', 'export'])]:
        sub = subs.add_parser(name); sub.add_argument('action', choices=actions); sub.add_argument('--as-of')
    args = p.parse_args(argv)
    w = Workspace(args.root, initialize=args.command == 'init')
    if args.command == 'init':
        result = {'workspace': str(w.root), 'database': str(w.db_path)}
    elif args.command == 'sources':
        if args.action == 'add':
            if not args.json: p.error('sources add requires --json')
            result = {'source_id': w.source(**json.loads(args.json.read_text(encoding='utf-8')))}
        else:
            result = w.list('source') if args.action == 'list' else w.verify()
    elif args.command in {'ingest', 'import'}:
        kw = {'collected_at': args.collected_at}
        if args.kind == 'rss':
            result = ingest_feed(w, args.file, args.source_id, **kw)
        elif args.kind == 'pdf':
            result = ingest_pdf(w, args.file, args.source_id, published_at=args.published_at, **kw)
        elif args.kind in {'market', 'market-data'}:
            if not args.asset_id: p.error('market import requires --asset-id')
            doc = w.ingest_file(args.file, args.source_id, **kw)
            with args.file.open(encoding='utf-8-sig', newline='') as f:
                rows = list(csv.DictReader(f))
            result = [w.market_bar(args.asset_id, row, document_id=doc, **kw) for row in rows]
        else:
            result = {'document_id': w.ingest_file(args.file, args.source_id, published_at=args.published_at, **kw)}
    elif args.command in {'events', 'entities'}:
        result = w.list('event' if args.command == 'events' else 'entity')
    elif args.command == 'record':
        data = json.loads(args.json.read_text(encoding='utf-8'))
        method = data.pop('method')
        if method not in {'entity', 'asset', 'evidence', 'opinion', 'relation', 'revise', 'event', 'observation', 'hypothesis', 'case', 'location'}:
            p.error('unsupported record method')
        if method in {'event', 'observation', 'hypothesis', 'case', 'location'}:
            result = w.put(method, **data)
        else:
            result = getattr(w, method)(**data)
    elif args.command == 'graph':
        graph = build_graph(w, args.as_of)
        result = {'nodes': graph.number_of_nodes(), 'edges': graph.number_of_edges(), 'as_of': args.as_of}
        if args.action == 'build':
            import networkx as nx
            path = w.safe_path('data/graph/graph.json'); path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(nx.node_link_data(graph), ensure_ascii=False, sort_keys=True, indent=2), encoding='utf-8')
    elif args.command in {'data', 'provenance'}:
        result = w.verify()
        if not result['ok']:
            print(json.dumps(result, ensure_ascii=False)); return 1
    elif args.command == 'report' and args.action == 'export':
        result = export(w, as_of=args.as_of)
    else:
        result = status(w)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, PermissionError, OSError) as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(2)
