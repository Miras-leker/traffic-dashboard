#!/usr/bin/env python3
"""Pulls the ad sets that are live right now from the Facebook Marketing API and writes fb_live.json.

Run by .github/workflows/fb_live.yml. Needs two repository secrets:
  FB_TOKEN       — system user token with ads_read on the ad account
  FB_AD_ACCOUNT  — ad account id, with or without the act_ prefix
Optional env: FB_API_VERSION (default v23.0), FB_EXCLUDE (regex for campaign names to skip, default vacancies).
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API = 'https://graph.facebook.com/' + os.environ.get('FB_API_VERSION', 'v23.0')
TOKEN = os.environ.get('FB_TOKEN', '').strip()
ACCOUNT = os.environ.get('FB_AD_ACCOUNT', '').strip()
EXCLUDE = re.compile(os.environ.get('FB_EXCLUDE') or r'ваканс|vakans|vacanc', re.I)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'fb_live.json')

# Which action counts as a lead depends on what the ad set optimises for; first match wins (no summing — "lead" already aggregates).
LEAD_ACTIONS = {
    'CONVERSATIONS': ['onsite_conversion.messaging_conversation_started_7d'],
    'LEAD_GENERATION': ['onsite_conversion.lead_grouped', 'lead'],
    'QUALITY_LEAD': ['onsite_conversion.lead_grouped', 'lead'],
    'OFFSITE_CONVERSIONS': ['offsite_conversion.fb_pixel_lead', 'lead'],
}
LEAD_FALLBACK = ['lead', 'onsite_conversion.lead_grouped', 'offsite_conversion.fb_pixel_lead', 'onsite_conversion.messaging_conversation_started_7d']


def get(path, **params):
    """GET with paging; returns the concatenated data list."""
    params['access_token'] = TOKEN
    url = f'{API}/{path}?{urllib.parse.urlencode(params)}'
    out = []
    while url:
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                body = json.load(r)
        except urllib.error.HTTPError as e:
            err = json.loads(e.read() or b'{}').get('error', {})
            sys.exit(f'Facebook API error on {path}: {err.get("message") or e}')
        out += body.get('data', [])
        url = body.get('paging', {}).get('next')
    return out


def leads_of(actions, goal):
    by_type = {a['action_type']: float(a['value']) for a in actions or []}
    for t in LEAD_ACTIONS.get(goal, []) + LEAD_FALLBACK:
        if t in by_type:
            return int(by_type[t])
    return 0


def main():
    if not TOKEN or not ACCOUNT:
        sys.exit('FB_TOKEN and FB_AD_ACCOUNT must be set')
    act = ACCOUNT if ACCOUNT.startswith('act_') else 'act_' + ACCOUNT
    live = json.dumps(['ACTIVE'])

    adsets = get(f'{act}/adsets', fields='id,name,daily_budget,optimization_goal,campaign{name}', effective_status=live, limit=200)
    adsets = [a for a in adsets if not EXCLUDE.search(a.get('campaign', {}).get('name', ''))]
    ids = [a['id'] for a in adsets]

    ads = get(f'{act}/ads', fields='name,adset_id', effective_status=live, limit=500)
    ads_by_set = {}
    for ad in ads:
        ads_by_set.setdefault(ad['adset_id'], []).append(ad['name'])

    week, today = {}, {}
    if ids:
        flt = json.dumps([{'field': 'adset.id', 'operator': 'IN', 'value': ids}])
        # last_7d = 7 full days before today; time_increment=1 so we can count the days the ad set actually spent.
        for r in get(f'{act}/insights', level='adset', date_preset='last_7d', time_increment=1, fields='adset_id,spend,actions', filtering=flt, limit=500):
            week.setdefault(r['adset_id'], []).append(r)
        for r in get(f'{act}/insights', level='adset', date_preset='today', fields='adset_id,spend,actions', filtering=flt, limit=500):
            today[r['adset_id']] = r

    rows = []
    for a in adsets:
        goal = a.get('optimization_goal', '')
        days = [d for d in week.get(a['id'], []) if float(d.get('spend') or 0) > 0]
        spend = sum(float(d['spend']) for d in days)
        leads = sum(leads_of(d.get('actions'), goal) for d in days)
        t = today.get(a['id'], {})
        t_spend, t_leads = float(t.get('spend') or 0), leads_of(t.get('actions'), goal)
        rows.append({
            'id': a['id'],
            'adset': a['name'],
            'campaign': a.get('campaign', {}).get('name', ''),
            'ads': sorted(ads_by_set.get(a['id'], []), key=lambda n: [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', n)]),
            'daily_budget': int(a['daily_budget']) / 100 if a.get('daily_budget') else None,
            'days': len(days),
            'spend_7d': round(spend, 2),
            'leads_7d': leads,
            'avg_spend_day': round(spend / len(days), 2) if days else None,
            'cpl': round(spend / leads, 2) if leads else None,
            'spend_today': round(t_spend, 2),
            'leads_today': t_leads,
        })
    rows.sort(key=lambda r: -(r['spend_7d'] + r['spend_today']))

    data = {'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'period': 'last_7d', 'adsets': rows}
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write('\n')
    print(f'{len(rows)} live ad sets written to fb_live.json')


if __name__ == '__main__':
    main()
