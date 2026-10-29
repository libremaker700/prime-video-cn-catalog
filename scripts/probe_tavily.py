"""One basic search; report public evidence without changing the catalogue."""
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests


def main():
    key = os.environ.get('TAVILY_API_KEY', '').strip()
    report = {'checked_at': datetime.now(timezone.utc).isoformat(),
              'query': 'Vinland Saga 2019 电视动画 中文名 国内译名',
              'catalogue_modified': False, 'status': 'failed', 'requests': 0}
    if not key:
        report['failure'] = 'missing_secret'
    else:
        try:
            report['requests'] = 1
            response = requests.post('https://api.tavily.com/search',
                headers={'Authorization': 'Bearer ' + key},
                json={'query': report['query'], 'search_depth': 'basic',
                      'auto_parameters': False, 'max_results': 8,
                      'include_answer': False, 'include_raw_content': False,
                      'include_images': False, 'include_usage': True}, timeout=(15, 60))
            report['http_status'] = response.status_code
            if response.status_code != 200:
                report['failure'] = 'search_http_error'
            else:
                payload = response.json()
                report['usage'] = payload.get('usage')
                report['results'] = [{k: result.get(k) for k in ('title', 'url', 'content', 'score')} for result in payload.get('results', [])]
                evidence = json.dumps(report['results'], ensure_ascii=False)
                report['name_mentions'] = {name: name in evidence for name in ('海盗战记','冰海战记','海盜戰記','冰海戰記')}
                report['status'] = 'search_succeeded' if report['results'] else 'empty_results'
                report['identity_verified'] = False
        except (requests.RequestException, ValueError) as exc:
            report['failure'] = 'network_or_response_error'
            report['error_type'] = type(exc).__name__
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if key:
        text = text.replace(key, '[REDACTED]')
    text = re.sub(r'tvly-[A-Za-z0-9_-]{12,}', '[REDACTED]', text)
    Path(os.environ.get('TAVILY_PROBE_REPORT', 'tavily-probe-report.json')).write_text(text, encoding='utf-8')
    print(text)
    return 0 if report['status'] == 'search_succeeded' else 1


if __name__ == '__main__':
    sys.exit(main())
