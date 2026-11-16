"""Reviewed, replaceable adapters: local bytes first, no implicit network calls."""
from pathlib import Path
from .core import timestamp

EXPLICIT_NETWORK_AUTHORIZATION = False
NETWORK_ACCESS = 'DISABLED'
LIVE_INGESTION = 'DISABLED'
AUTO_TRADING = 'DISABLED'

class NetworkDisabled(PermissionError):
    pass

class RemoteAdapter:
    def fetch(self, *args, **kwargs):
        raise NetworkDisabled('network ingestion disabled; use an explicitly authorised local export')

class AkshareAdapter(RemoteAdapter):
    """Normalise a caller-supplied AKShare-shaped DataFrame, without calling endpoints."""
    fields = {'日期': 'trade_date', '开盘': 'open', '最高': 'high', '最低': 'low', '收盘': 'close', '成交量': 'volume', '成交额': 'amount'}
    def normalize(self, frame, *, code, exchange, asset_type, adjustment, market_as_of):
        timestamp(market_as_of)
        missing = set(self.fields) - set(frame.columns)
        if missing:
            raise ValueError(f'AKShare export missing columns: {sorted(missing)}')
        rows = frame.rename(columns=self.fields)[list(self.fields.values())].to_dict('records')
        # No implicit unit conversion: supplier-specific volume units require declaration.
        for row in rows:
            row['trade_date'] = str(row['trade_date'])[:10]
            row.update(code=code, exchange=exchange, asset_type=asset_type, adjustment=adjustment,
                       market_as_of=market_as_of, sample_kind='LOCAL_AKSHARE_EXPORT_UNVERIFIED')
        return rows

def parse_feed(path):
    """Pass bytes, never a URL, to feedparser; RSS summaries remain summaries."""
    import feedparser
    parsed = feedparser.parse(Path(path).read_bytes())
    if parsed.bozo or parsed.version not in {'rss20', 'rss10', 'rss091n', 'rss092', 'atom10', 'atom03'}:
        raise ValueError('invalid RSS/Atom fixture; no route fallback attempted')
    return [{'external_id': entry.get('id') or entry.get('link') or entry.get('title'),
             'title': entry.get('title'), 'url': entry.get('link'), 'published_original': entry.get('published'),
             'summary': entry.get('summary'), 'full_text_available': False, 'fact_status': 'UNVERIFIED'} for entry in parsed.entries]

def ingest_feed(workspace, path, source_id, *, collected_at):
    try:
        doc = workspace.ingest_file(path, source_id, collected_at=collected_at, capture_method='LOCAL_FEED')
        entries = parse_feed(path)
        result = []
        for entry in entries:
            published = None
            if entry['published_original']:
                from email.utils import parsedate_to_datetime
                try:
                    published = parsedate_to_datetime(entry['published_original']).isoformat()
                    timestamp(published)
                except (ValueError, TypeError):
                    try:
                        timestamp(entry['published_original']); published = entry['published_original']
                    except ValueError:
                        published = None
            result.append(workspace.put('observation', [doc, entry['external_id']], {'observation_type': 'FEED_ENTRY', **entry},
                collected_at=collected_at, published_at=published, source_id=source_id, document_id=doc))
        return result
    except Exception as exc:
        workspace.failure('rss', collected_at, exc)
        raise

def parse_pdf_native(path):
    """Docling NativePdfPipeline: no OCR/layout model weights or remote service."""
    from io import BytesIO
    from docling.datamodel.base_models import InputFormat, DocumentStream
    from docling.backend.pypdfium2_backend import PyPdfiumDocumentBackend
    from docling.datamodel.pipeline_options import NativePdfPipelineOptions
    from docling.document_converter import DocumentConverter, NativePdfFormatOption
    options = NativePdfPipelineOptions()
    options.generate_page_images = False
    converter = DocumentConverter(allowed_formats=[InputFormat.PDF], format_options={
        InputFormat.PDF: NativePdfFormatOption(pipeline_options=options, backend=PyPdfiumDocumentBackend)})
    # Explicit PDFium backend avoids docling-parse Windows Unicode resource-path bug.
    result = converter.convert(DocumentStream(name='local-evidence.pdf', stream=BytesIO(Path(path).read_bytes())))
    text = result.document.export_to_markdown()
    if not text.strip():
        raise ValueError('PDF has no native text; OCR is not enabled')
    blocks = [{'text': item.text, 'provenance': [p.model_dump(mode='json') for p in item.prov]}
              for item in result.document.texts]
    if not blocks or not any(b['provenance'] for b in blocks):
        raise ValueError('PDF extraction did not preserve page provenance')
    return {'text': text, 'blocks': blocks, 'parser': 'Docling NativePdfPipeline', 'limitations': 'no OCR/table/layout reconstruction'}

def ingest_pdf(workspace, path, source_id, *, collected_at, published_at=None):
    from .core import canonical
    doc = workspace.ingest_file(path, source_id, collected_at=collected_at, published_at=published_at)
    try:
        result = parse_pdf_native(path)
        target = workspace.safe_path(f'data/normalized/{doc}.json')
        target.parent.mkdir(parents=True, exist_ok=True)
        data = canonical(result)
        if target.exists() and target.read_text(encoding='utf-8') != data:
            raise ValueError('parser output changed; explicit versioned extraction required')
        if not target.exists():
            target.write_text(data, encoding='utf-8')
        return workspace.put('observation', [doc, 'docling-native-v1'], {'observation_type': 'DOCUMENT_EXTRACTION',
            'original_document_id': doc, 'normalized_path': target.relative_to(workspace.root).as_posix(), **result},
            collected_at=collected_at, published_at=published_at, source_id=source_id, document_id=doc)
    except Exception as exc:
        workspace.failure('docling', collected_at, exc)
        raise

def build_graph(workspace, as_of=None):
    import networkx as nx
    graph = nx.MultiDiGraph()
    rows = workspace.list(as_of=as_of)
    for row in rows:
        if row['kind'] != 'relation':
            graph.add_node(row['id'], kind=row['kind'], available_at=row['available_at'])
    for row in rows:
        if row['kind'] == 'relation':
            p = row['payload']
            if as_of and (p['valid_from']['utc'] > timestamp(as_of)['utc'] or (p['valid_to'] and p['valid_to']['utc'] < timestamp(as_of)['utc'])):
                continue
            if p['source_entity'] in graph and p['target_entity'] in graph and p['evidence_id'] in graph:
                graph.add_edge(p['source_entity'], p['target_entity'], key=row['id'], **p)
    return graph
