from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

FACT_STATES = {'VERIFIED', 'PARTIALLY_VERIFIED', 'UNVERIFIED', 'DISPUTED'}
OPINION_STATES = {'FORECAST', 'INTERPRETATION', 'COMMENTARY', 'RUMOR'}
RESEARCH_STATES = {'REGISTERED', 'RESEARCH_ONLY', 'BACKTESTED', 'OUT_OF_SAMPLE', 'PROSPECTIVE', 'REJECTED'}
RECORD_TYPES = {'source', 'document', 'entity', 'asset', 'event', 'observation', 'opinion', 'hypothesis', 'evidence', 'market_bar', 'relation', 'case', 'location'}
BLOCKED_TREES = {'quant_v4_6', 'cheetah_v4.2_fixed', 'quant_v4.5.1_fixed', 'cheetah_ui', '_design'}

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def digest(data: bytes):
    return hashlib.sha256(data).hexdigest()

def stable_id(kind, identity):
    return f'{kind}-{digest(canonical(identity).encode())[:24]}'

def timestamp(value: str):
    if not isinstance(value, str):
        raise ValueError('explicit timezone-aware timestamp required')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.utcoffset() is None:
        raise ValueError('naive timestamp is forbidden')
    return {'utc': parsed.astimezone(timezone.utc).isoformat(), 'original': value, 'timezone': str(parsed.tzinfo)}

class ConflictError(ValueError):
    pass

class Workspace:
    """Writes are constrained to a dedicated marked MIR root, not a name substring.

    This is application-level containment, not an OS security sandbox.
    """
    def __init__(self, root, *, initialize=False):
        self.root = Path(root).resolve()
        if any(p.lower() in BLOCKED_TREES for p in self.root.parts):
            raise ValueError('protected Cheetah project cannot be a MIR workspace')
        marker = self.root / 'mir_workspace.json'
        if not marker.is_file():
            if not initialize:
                raise ValueError('run mir init in a dedicated directory first')
            allowed = {'.venv', 'src', 'tests', 'tools', 'docs', 'configs', 'reports', 'pyproject.toml', '.gitignore', '.env.example', 'README.md', 'THIRD_PARTY_NOTICES.md', 'requirements.lock.txt'}
            if self.root.exists() and any(p.name not in allowed for p in self.root.iterdir()):
                raise ValueError('refusing to initialize an occupied non-MIR directory')
            self.root.mkdir(parents=True, exist_ok=True)
            marker.write_text(canonical({'application': 'cheetah-mir', 'schema_version': 1}), encoding='utf-8')
        if json.loads(marker.read_text(encoding='utf-8')) != {'application': 'cheetah-mir', 'schema_version': 1}:
            raise ValueError('invalid workspace marker')
        self.db_path = self.safe_path('data/database/mir.sqlite')
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS records (
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, identity TEXT NOT NULL, payload TEXT NOT NULL,
              source_id TEXT REFERENCES records(id), document_id TEXT REFERENCES records(id),
              available_at TEXT NOT NULL, collected_at TEXT NOT NULL, content_hash TEXT NOT NULL,
              UNIQUE(kind,identity));
            CREATE TABLE IF NOT EXISTS audit (
              seq INTEGER PRIMARY KEY, action TEXT NOT NULL, record_id TEXT NOT NULL,
              at TEXT NOT NULL, details TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS failures (
              id INTEGER PRIMARY KEY, adapter TEXT NOT NULL, at TEXT NOT NULL, error_type TEXT NOT NULL, message TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS conflicts (
              id INTEGER PRIMARY KEY, record_id TEXT NOT NULL, at TEXT NOT NULL, existing_hash TEXT NOT NULL, incoming_hash TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS immutable_records_update BEFORE UPDATE ON records
              BEGIN SELECT RAISE(ABORT,'append a revision; original records are immutable'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_records_delete BEFORE DELETE ON records
              BEGIN SELECT RAISE(ABORT,'original records are immutable'); END;
            ''')
            for view, kind in {'trader_profiles': 'entity', 'trader_documents': 'document', 'trader_opinions': 'opinion', 'trader_predictions': 'opinion', 'trader_outcomes': 'observation', 'company_profiles': 'entity', 'company_events': 'event', 'industry_entities': 'entity', 'company_relations': 'relation', 'related_assets': 'asset', 'macro_events': 'event', 'policy_events': 'event', 'industry_events': 'event', 'geopolitical_events': 'event', 'event_locations': 'location', 'market_breadth_daily': 'observation', 'sector_daily': 'observation', 'limit_statistics_daily': 'observation'}.items():
                c.execute(f"CREATE VIEW IF NOT EXISTS {view} AS SELECT * FROM records WHERE kind='{kind}'")
            for view, asset_type in {'index_daily': 'INDEX', 'equity_daily': 'EQUITY', 'etf_daily': 'ETF'}.items():
                c.execute(f"CREATE VIEW IF NOT EXISTS {view} AS SELECT * FROM records WHERE kind='market_bar' AND json_extract(payload,'$.asset_type')='{asset_type}'")
            # Domain-specific views select only their declared subtype, not all JSON records.
            predicates = {
                'trader_profiles': "kind='entity' AND json_extract(payload,'$.entity_type')='PERSON'",
                'trader_documents': "kind='document' AND id IN (SELECT document_id FROM records WHERE kind='opinion')",
                'trader_predictions': "kind='opinion' AND json_extract(payload,'$.opinion_state')='FORECAST'",
                'trader_outcomes': "kind='observation' AND json_extract(payload,'$.observation_type')='OPINION_OUTCOME'",
                'company_profiles': "kind='entity' AND json_extract(payload,'$.entity_type')='COMPANY'",
                'industry_entities': "kind='entity' AND json_extract(payload,'$.entity_type')='INDUSTRY'",
                'company_relations': "kind='relation' AND json_extract(payload,'$.relation_type') IN ('HAS_BUSINESS_RELATION','BELONGS_TO')",
                'related_assets': "kind='asset' AND json_extract(payload,'$.company_id') IS NOT NULL",
                'company_events': "kind='event' AND json_extract(payload,'$.event_type')='COMPANY'",
            }
            for view, subtype in {'macro_events': 'MACRO', 'policy_events': 'POLICY', 'industry_events': 'INDUSTRY', 'geopolitical_events': 'GEOPOLITICAL'}.items():
                predicates[view] = f"kind='event' AND json_extract(payload,'$.event_type')='{subtype}'"
            for view, subtype in {'market_breadth_daily': 'MARKET_BREADTH', 'sector_daily': 'SECTOR', 'limit_statistics_daily': 'LIMIT_STATISTICS'}.items():
                predicates[view] = f"kind='observation' AND json_extract(payload,'$.observation_type')='{subtype}'"
            for view, condition in predicates.items():
                c.execute(f'DROP VIEW IF EXISTS {view}')
                c.execute(f'CREATE VIEW {view} AS SELECT * FROM records WHERE {condition}')

    def safe_path(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError('output escapes MIR workspace')
        return path

    @contextmanager
    def db(self):
        with sqlite3.connect(self.db_path) as c:
            c.row_factory = sqlite3.Row
            c.execute('PRAGMA foreign_keys=ON')
            yield c

    def get(self, rid):
        with self.db() as c:
            row = c.execute('SELECT * FROM records WHERE id=?', (rid,)).fetchone()
        if row is None:
            raise KeyError(rid)
        value = dict(row)
        value['payload'] = json.loads(value['payload'])
        return value

    def list(self, kind=None, as_of=None):
        clauses, params = [], []
        if kind:
            clauses.append('kind=?'); params.append(kind)
        if as_of:
            clauses.append('available_at<=?'); params.append(timestamp(as_of)['utc'])
        query = 'SELECT id FROM records' + (' WHERE ' + ' AND '.join(clauses) if clauses else '') + ' ORDER BY id'
        with self.db() as c:
            ids = [r[0] for r in c.execute(query, params)]
        return [self.get(rid) for rid in ids]

    def put(self, kind, identity, payload, *, collected_at, published_at=None, source_id=None, document_id=None):
        if kind not in RECORD_TYPES:
            raise ValueError('unsupported record kind')
        captured = timestamp(collected_at)
        published = timestamp(published_at) if published_at else None
        available = max(captured['utc'], published['utc'] if published else captured['utc'])
        for ref, required_kind in ((source_id, 'source'), (document_id, 'document')):
            if ref:
                record = self.get(ref)
                if record['kind'] != required_kind:
                    raise ValueError(f'{required_kind} reference expected')
                available = max(available, record['available_at'])
        for key in ('person_id', 'evidence_id', 'asset_id', 'company_id', 'source_entity', 'target_entity', 'original_document_id', 'previous_version_id', 'event_id', 'market_bar_id', 'opinion_id', 'observation_id', 'case_id'):
            if payload.get(key):
                available = max(available, self.get(payload[key])['available_at'])
        if payload.get('fact_status', 'UNVERIFIED') not in FACT_STATES:
            raise ValueError('invalid fact_status')
        if payload.get('fact_status') == 'VERIFIED' or payload.get('review_status') == 'VERIFIED':
            raise ValueError('verified status requires a separate human review workflow')
        if kind == 'hypothesis' and payload.get('research_state') not in RESEARCH_STATES:
            raise ValueError('invalid research_state')
        value = dict(payload, collected_time=captured, published_time=published, decision_status='RESEARCH_ONLY')
        if payload.get('event_at'):
            value['event_time'] = timestamp(payload['event_at'])
        if kind in {'event', 'observation', 'opinion', 'market_bar', 'relation', 'evidence', 'document', 'location'} and not (source_id or document_id):
            raise ValueError('source or original document required')
        rid, body, key = stable_id(kind, identity), canonical(value), canonical(identity)
        with self.db() as c:
            old = c.execute('SELECT payload,content_hash FROM records WHERE id=?', (rid,)).fetchone()
            if old:
                # Acquisition timestamps do not create duplicate content or timestamp churn.
                before = json.loads(old['payload']); incoming = dict(value)
                before.pop('collected_time', None); incoming.pop('collected_time', None)
                if canonical(before) == canonical(incoming):
                    return rid
                c.execute('INSERT INTO conflicts(record_id,at,existing_hash,incoming_hash) VALUES (?,?,?,?)', (rid, captured['utc'], old['content_hash'], digest(body.encode())))
            else:
                c.execute('INSERT INTO records VALUES (?,?,?,?,?,?,?,?,?)', (rid, kind, key, body, source_id, document_id, available, captured['utc'], digest(body.encode())))
                c.execute('INSERT INTO audit(action,record_id,at,details) VALUES (?,?,?,?)', ('INSERT', rid, captured['utc'], canonical({'kind': kind})))
                return rid
        raise ConflictError(f'conflict retained without overwriting {rid}')

    def failure(self, adapter, at, exc):
        with self.db() as c:
            c.execute('INSERT INTO failures(adapter,at,error_type,message) VALUES (?,?,?,?)', (adapter, timestamp(at)['utc'], type(exc).__name__, str(exc)[:500]))

    def source(self, name, url, tier, *, collected_at, rights='Metadata and user-authorised local research only', **metadata):
        if tier not in {'A', 'B'}:
            raise ValueError('source tier must be A or B')
        return self.put('source', [name, url], {'name': name, 'url': url, 'tier': tier, 'rights': rights, 'fact_status': 'UNVERIFIED', **metadata}, collected_at=collected_at)

    def revise(self, record_id, changes, *, collected_at):
        old = self.get(record_id)
        payload = {k: v for k, v in old['payload'].items() if k not in {'collected_time', 'published_time', 'decision_status'}}
        payload.update(changes)
        payload['previous_version_id'] = record_id
        return self.put(old['kind'], [record_id, digest(canonical(changes).encode())], payload,
                        collected_at=collected_at, published_at=(old['payload'].get('published_time') or {}).get('original'),
                        source_id=old['source_id'], document_id=old['document_id'])

    def ingest_file(self, path, source_id, *, collected_at, published_at=None, title=None, capture_method='MANUAL_IMPORT'):
        timestamp(collected_at)
        if published_at: timestamp(published_at)
        source = self.get(source_id)
        if source['kind'] != 'source':
            raise ValueError('invalid source')
        path = Path(path)
        data = path.read_bytes(); sha = digest(data)
        existing_id = stable_id('document', [source_id, sha])
        try:
            existing = self.get(existing_id)
        except KeyError:
            existing = None
        if existing:
            archived = self.safe_path(existing['payload']['raw_path'])
            if not archived.is_file() or digest(archived.read_bytes()) != sha:
                raise ValueError('archived bytes corrupted')
            previous_publication = (existing['payload'].get('published_time') or {}).get('original')
            if published_at is not None and published_at != previous_publication:
                original = {k: v for k, v in existing['payload'].items() if k not in {'collected_time', 'published_time', 'decision_status'}}
                return self.put('document', [source_id, sha], original, collected_at=collected_at,
                    published_at=published_at, source_id=source_id)
            return existing_id
        target = self.safe_path(f'data/raw/{sha[:2]}/{sha}{path.suffix.lower()}')
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and digest(target.read_bytes()) != sha:
            raise ValueError('archived bytes corrupted')
        if not target.exists():
            with target.open('xb') as f:
                f.write(data)
        # Same bytes from different sources share archive identity, not source claims.
        return self.put('document', [source_id, sha], {'title': title or path.name, 'raw_path': target.relative_to(self.root).as_posix(),
            'raw_sha256': sha, 'media_type': path.suffix.lower(), 'capture_method': capture_method,
            'duplicate_group': sha, 'original_path': str(path.resolve()), 'fact_status': 'UNVERIFIED'},
            collected_at=collected_at, published_at=published_at, source_id=source_id)

    def entity(self, name, entity_type, *, collected_at, document_id=None):
        return self.put('entity', [entity_type, name], {'name': name, 'entity_type': entity_type, 'fact_status': 'UNVERIFIED'}, collected_at=collected_at, document_id=document_id)

    def evidence(self, document_id, quote, location, *, collected_at):
        doc = self.get(document_id)
        if not quote.strip() or not location.strip():
            raise ValueError('quote and exact source location required')
        raw = self.safe_path(doc['payload']['raw_path'])
        if doc['payload']['media_type'] in {'.txt', '.md', '.html', '.csv', '.xml'}:
            if quote not in raw.read_text(encoding='utf-8-sig'):
                raise ValueError('quote is not present in original source')
        else:
            raise ValueError('binary evidence needs a reviewed extracted-text document with original parent reference')
        return self.put('evidence', [document_id, quote, location], {'quote': quote, 'location': location, 'fact_status': 'UNVERIFIED'}, collected_at=collected_at, source_id=doc['source_id'], document_id=document_id)

    def opinion(self, person_id, evidence_id, *, opinion_state, target, horizon, collected_at, published_at):
        if opinion_state not in OPINION_STATES:
            raise ValueError('invalid opinion state')
        person, ev = self.get(person_id), self.get(evidence_id)
        if person['kind'] != 'entity' or person['payload']['entity_type'] != 'PERSON' or ev['kind'] != 'evidence':
            raise ValueError('person and original evidence required')
        return self.put('opinion', [person_id, evidence_id, target], {'person_id': person_id, 'evidence_id': evidence_id,
            'opinion_state': opinion_state, 'target': target, 'horizon': horizon, 'attribution': 'AUTHOR_EXPLICIT_VIEW',
            'quote': ev['payload']['quote'], 'fact_status': 'UNVERIFIED'}, collected_at=collected_at, published_at=published_at,
            source_id=ev['source_id'], document_id=ev['document_id'])

    def asset(self, code, exchange, asset_type, *, collected_at, document_id, company_id=None, listing_status='UNKNOWN'):
        if asset_type not in {'INDEX', 'EQUITY', 'ETF', 'FUND_NAV', 'BOND', 'OTHER'}:
            raise ValueError('unknown asset identity')
        if listing_status != 'LISTED' and asset_type in {'EQUITY', 'ETF'}:
            raise ValueError('unlisted or unknown identity cannot receive a security mapping')
        if not code or not exchange:
            raise ValueError('code and exchange required')
        if exchange in {'SSE', 'SZSE', 'BSE'} and (not isinstance(code, str) or len(code) != 6 or not code.isdigit()):
            raise ValueError('Chinese security code must be an exact six-digit string')
        if company_id and self.get(company_id)['payload'].get('entity_type') != 'COMPANY':
            raise ValueError('company identity required')
        return self.put('asset', [code, exchange, asset_type], {'code': code, 'exchange': exchange, 'asset_type': asset_type,
            'company_id': company_id, 'listing_status': listing_status, 'fact_status': 'UNVERIFIED'}, collected_at=collected_at, document_id=document_id)

    def market_bar(self, asset_id, row, *, document_id, collected_at):
        asset = self.get(asset_id)['payload']; doc = self.get(document_id)
        if asset['asset_type'] == 'FUND_NAV':
            raise ValueError('NAV cannot enter OHLC market bars')
        from datetime import date
        day = date.fromisoformat(row['trade_date']).isoformat()
        for field in ('code', 'exchange', 'asset_type'):
            if row.get(field) != asset[field]:
                raise ValueError(f'{field} identity mismatch')
        prices = {k: None if row.get(k) in ('', None) else float(row[k]) for k in ('open', 'high', 'low', 'close', 'volume', 'amount')}
        if any(v is not None and (not math.isfinite(v) or v < 0) for v in prices.values()):
            raise ValueError('non-finite or negative market value')
        if any(prices[k] is None for k in ('open', 'high', 'low', 'close')):
            raise ValueError('OHLC missing; retain as missing observation, not a price bar')
        if prices['low'] > min(prices['open'], prices['close']) or prices['high'] < max(prices['open'], prices['close']):
            raise ValueError('invalid OHLC bounds')
        if row.get('adjustment') not in {'NONE', 'QFQ', 'HFQ'}:
            raise ValueError('adjustment required')
        market_as_of = timestamp(row['market_as_of'])
        if datetime.fromisoformat(market_as_of['original']).date().isoformat() != day:
            raise ValueError('market_as_of does not match trade_date')
        return self.put('market_bar', [asset_id, day, row['adjustment'], doc['source_id']], {**asset, **prices, 'trade_date': day,
            'asset_id': asset_id, 'market_as_of': market_as_of, 'adjustment': row['adjustment'], 'sample_kind': row.get('sample_kind', 'LOCAL_UNVERIFIED'),
            'quality': 'PARTIAL' if any(v is None for v in prices.values()) else 'COMPLETE', 'fact_status': 'UNVERIFIED'},
            collected_at=collected_at, published_at=row['market_as_of'], source_id=doc['source_id'], document_id=document_id)

    def relation(self, source_entity, target_entity, relation_type, evidence_id, *, collected_at, valid_from,
                 valid_to=None, confidence=None, review_status='UNVERIFIED'):
        if review_status not in FACT_STATES:
            raise ValueError('invalid relation review status')
        if review_status == 'VERIFIED':
            raise ValueError('v0.1 importer cannot auto-verify relations; separate human review required')
        if confidence is not None and (not math.isfinite(confidence) or not 0 <= confidence <= 1):
            raise ValueError('confidence outside [0,1]')
        left, right, evidence = self.get(source_entity), self.get(target_entity), self.get(evidence_id)
        if evidence['kind'] != 'evidence' or not relation_type:
            raise ValueError('typed source-backed relationship required')
        start = timestamp(valid_from); end = timestamp(valid_to) if valid_to else None
        if end and end['utc'] < start['utc']:
            raise ValueError('inverted validity interval')
        return self.put('relation', [source_entity, target_entity, relation_type, evidence_id, valid_from], {
            'source_entity': source_entity, 'target_entity': target_entity, 'relation_type': relation_type,
            'evidence_id': evidence_id, 'source_document_id': evidence['document_id'], 'valid_from': start, 'valid_to': end,
            'confidence': confidence, 'review_status': review_status, 'fact_status': 'UNVERIFIED'}, collected_at=collected_at,
            source_id=evidence['source_id'], document_id=evidence['document_id'])

    def verify(self):
        errors = []
        records = self.list()
        by_id = {r['id']: r for r in records}
        for row in records:
            if digest(canonical(row['payload']).encode()) != row['content_hash']:
                errors.append([row['id'], 'record checksum mismatch'])
            if row['kind'] == 'document':
                raw = self.safe_path(row['payload']['raw_path'])
                if not raw.is_file() or digest(raw.read_bytes()) != row['payload']['raw_sha256']:
                    errors.append([row['id'], 'raw checksum mismatch'])
            for field in ('source_id', 'document_id'):
                if row[field] and row[field] not in by_id:
                    errors.append([row['id'], f'missing {field}'])
        return {'records': len(records), 'errors': errors, 'ok': not errors}
