"""Read a public Google result in an ordinary GitHub-hosted Chromium browser.

No accounts, API keys, stealth changes or CAPTCHA solving. Never publish data.
"""
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from playwright.async_api import async_playwright


async def main():
    output = Path(os.environ.get('GOOGLE_PROBE_OUTPUT', 'google-probe-output'))
    output.mkdir(parents=True, exist_ok=True)
    query = 'Vinland Saga 在国内叫什么'
    report = {'checked_at': datetime.now(timezone.utc).isoformat(),
              'query': query, 'catalogue_modified': False,
              'status': 'failed', 'browser': 'ordinary headless Chromium',
              'uses_google_account': False, 'uses_api_key': False}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(locale='zh-CN', viewport={'width': 1440, 'height': 1100})
        page = await context.new_page()
        try:
            response = await page.goto('https://www.google.com/search?' + urlencode({'q': query, 'hl': 'zh-CN'}), wait_until='domcontentloaded', timeout=60000)
            report['http_status'] = response.status if response else None
            # Bound the wait for client-rendered search content.
            await page.wait_for_timeout(8000)
            report['url'] = page.url
            body = await page.locator('body').inner_text(timeout=15000)
            blocked = '/sorry/' in page.url or any(s in body.lower() for s in ('unusual traffic', '异常流量', 'not a robot', '不是机器人'))
            report['access_challenge'] = blocked
            report['consent_page'] = 'consent.google.' in page.url
            report['page_title'] = await page.title()
            report['body_text'] = body[:45000]
            report['overview_candidates'] = await page.evaluate('''() => {
                const matches = [...document.querySelectorAll('h1,h2,h3,[role="heading"]')]
                  .filter(e => /AI 概览|AI概览|AI Overview|AI overview/.test(e.innerText || ''));
                return matches.slice(0, 4).map(e => {
                    const candidates = [];
                    let parent = e;
                    for (let level = 0; parent && level < 6; level++, parent=parent.parentElement) {
                        const text=parent.innerText || '';
                        if(text.length > 80 && text.length < 18000) candidates.push({level,text,
                          links:[...parent.querySelectorAll('a[href]')].map(a=>({text:a.innerText,url:a.href})).slice(0,40)});
                    }
                    return {heading:e.innerText, candidates};
                });
            }''')
            report['links'] = await page.locator('a[href]').evaluate_all('els => els.map(a=>({text:a.innerText,url:a.href})).filter(a=>a.text && a.url.startsWith("https://")).slice(0,100)')
            report['name_mentions'] = {name: name in body for name in ('海盗战记', '冰海战记', '海盜戰記', '冰海戰記')}
            report['overview_heading_present'] = bool(report['overview_candidates'])
            if blocked:
                report['failure'] = 'google_access_challenge'
            elif report['consent_page']:
                report['failure'] = 'consent_required'
            elif report['overview_heading_present']:
                report['status'] = 'overview_detected_needs_source_review'
            elif any(report['name_mentions'].values()):
                report['status'] = 'ordinary_results_only'
            else:
                report['failure'] = 'no_overview_or_chinese_name'
            await page.screenshot(path=str(output/'google-search.png'))
            (output/'google-search.html').write_text(await page.content(), encoding='utf-8')
        except Exception as exc:
            report['failure'] = 'browser_or_network_error'
            report['error_type'] = type(exc).__name__
        finally:
            await browser.close()
    (output/'google-probe-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ('body_text','links','overview_candidates')}, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'overview_detected_needs_source_review' else 1


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
