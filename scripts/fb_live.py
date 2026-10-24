#!/usr/bin/env python3
"""Pulls the ad sets that are live right now from the Facebook Marketing API and writes fb_live.json.

Totals are for the whole life of each ad set (date_preset=maximum), per ad set and per ad.
Run by .github/workflows/fb_live.yml. Needs two repository secrets:
  FB_TOK         — system user token with ads_read on the ad account
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


class ApiError(Exception):
    pass


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
            raise ApiError(f'Facebook API error on {path}: {err.get("message") or e}')
        out += body.get('data', [])
        url = body.get('paging', {}).get('next')
    return out


def leads_of(actions, goal):
    by_type = {a['action_type']: float(a['value']) for a in actions or []}
    for t in LEAD_ACTIONS.get(goal, []) + LEAD_FALLBACK:
        if t in by_type:
            return int(by_type[t])
    return 0


def natural(name):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r'(\d+)', name)]


def main():
    if not TOKEN or not ACCOUNT:
        sys.exit('FB_TOKEN and FB_AD_ACCOUNT must be set')
    act = ACCOUNT if ACCOUNT.startswith('act_') else 'act_' + ACCOUNT
    live = json.dumps(['ACTIVE'])

    adsets = get(f'{act}/adsets', fields='id,name,daily_budget,optimization_goal,campaign{name}', effective_status=live, limit=200)
    adsets = [a for a in adsets if not EXCLUDE.search(a.get('campaign', {}).get('name', ''))]
    ids = [a['id'] for a in adsets]
    goal_of = {a['id']: a.get('optimization_goal', '') for a in adsets}

    per_day, per_ad, ads = {}, {}, []
    if ids:
        flt = json.dumps([{'field': 'adset.id', 'operator': 'IN', 'value': ids}])
        # All ads of the live ad sets, switched off ones too: their spend is part of the ad set's total.
        try:
            ads = get(f'{act}/ads', fields='id,name,adset_id,effective_status,creative.thumbnail_width(240).thumbnail_height(240){thumbnail_url}', filtering=flt, limit=500)
        except ApiError:
            ads = get(f'{act}/ads', fields='id,name,adset_id,effective_status,creative{thumbnail_url}', filtering=flt, limit=500)
        # date_preset=maximum = whole life; time_increment=1 at ad set level to count the days it actually spent.
        for r in get(f'{act}/insights', level='adset', date_preset='maximum', time_increment=1, fields='adset_id,spend,actions', filtering=flt, limit=500):
            per_day.setdefault(r['adset_id'], []).append(r)
        for r in get(f'{act}/insights', level='ad', date_preset='maximum', fields='ad_id,spend,actions', filtering=flt, limit=500):
            per_ad[r['ad_id']] = r

    ads_by_set = {}
    for ad in ads:
        goal = goal_of.get(ad['adset_id'], '')
        i = per_ad.get(ad['id'], {})
        spend, leads = float(i.get('spend') or 0), leads_of(i.get('actions'), goal)
        on = ad.get('effective_status') == 'ACTIVE'
        if not on and not spend:
            continue
        ads_by_set.setdefault(ad['adset_id'], []).append({
            'name': ad['name'],
            'on': on,
            'spend': round(spend, 2),
            'leads': leads,
            'cpl': round(spend / leads, 2) if leads else None,
            'thumb': ad.get('creative', {}).get('thumbnail_url'),
        })

    rows = []
    for a in adsets:
        goal = goal_of[a['id']]
        days = sorted((d for d in per_day.get(a['id'], []) if float(d.get('spend') or 0) > 0), key=lambda d: d['date_start'])
        spend = sum(float(d['spend']) for d in days)
        leads = sum(leads_of(d.get('actions'), goal) for d in days)
        rows.append({
            'id': a['id'],
            'adset': a['name'],
            'campaign': a.get('campaign', {}).get('name', ''),
            'ads': sorted(ads_by_set.get(a['id'], []), key=lambda x: (not x['on'], natural(x['name']))),
            'daily_budget': int(a['daily_budget']) / 100 if a.get('daily_budget') else None,
            'since': days[0]['date_start'] if days else None,
            'days': len(days),
            'spend': round(spend, 2),
            'leads': leads,
            'avg_spend_day': round(spend / len(days), 2) if days else None,
            'cpl': round(spend / leads, 2) if leads else None,
        })
    rows.sort(key=lambda r: -r['spend'])

    data = {'updated_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'period': 'maximum', 'adsets': rows}
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.write('\n')
    print(f'{len(rows)} live ad sets written to fb_live.json')


if __name__ == '__main__':
    try:
        main()
    except ApiError as e:
        sys.exit(str(e))
