"""Read only publisher operation identifiers. Never execute push/dispatch or print raw receipt/errors."""
import argparse
import hashlib
import json
from pathlib import Path
import re

parser = argparse.ArgumentParser()
parser.add_argument('--state', required=True, type=Path)
args = parser.parse_args()
raw = args.state.read_bytes()
state = json.loads(raw)
if not isinstance(state, dict):
    raise SystemExit('State is not an object; preserve and inspect privately')
result = {'state_sha256': hashlib.sha256(raw).hexdigest()}
for name in ['push_pending', 'push_force', 'dispatch_pending', 'dispatch_started']:
    value = state.get(name)
    result[name] = value if type(value) is bool else 'MISSING_OR_INVALID'
for name in ['pending_commit', 'published_commit']:
    value = state.get(name)
    result[name] = value if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{40,64}', value) else ('MISSING' if value is None else 'UNCLASSIFIED_PRESENT')
value = state.get('dispatch_id')
result['dispatch_id'] = value if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value) else ('MISSING' if value is None else 'UNCLASSIFIED_PRESENT')
paths = state.get('pending_targets')
result['pending_targets'] = paths if isinstance(paths, list) and all(isinstance(p, str) and re.fullmatch(r'[A-Za-z0-9_./-]+', p) and '..' not in Path(p).parts for p in paths) else ('MISSING' if paths is None else 'UNCLASSIFIED_PRESENT')
receipt = state.get('dispatch_receipt')
safe = {}
if isinstance(receipt, dict):
    if isinstance(receipt.get('databaseId'), int):
        safe['databaseId'] = receipt['databaseId']
    url = receipt.get('url')
    if isinstance(url, str) and re.fullmatch(r'https://github.com/acaiaishizhan/rss-ingest-action/actions/runs/[0-9]+', url):
        safe['url'] = url
elif isinstance(receipt, str) and re.fullmatch(r'https://github.com/acaiaishizhan/rss-ingest-action/actions/runs/[0-9]+', receipt):
    safe['url'] = receipt
result['dispatch_receipt'] = safe or ('MISSING' if receipt is None else 'PRESENT_UNCLASSIFIED_DO_NOT_RESUBMIT')
result['dispatch_rejection_present'] = bool(state.get('dispatch_rejection'))
result['merged_commits_present'] = bool(state.get('merged_commits'))
print(json.dumps(result, indent=2))
