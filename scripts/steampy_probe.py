"""Read-only anonymous SteamPY diagnostic. Never signs in or places orders."""
from __future__ import annotations

import argparse
import json
import urllib.parse
from pathlib import Path

try:
    from .providers import JsonClient, now_iso
except ImportError:
    from providers import JsonClient, now_iso


def probe(appid, output=None):
    client = JsonClient(timeout=20, attempts=1)
    app = client.get(f'https://store.steampowered.com/api/appdetails?appids={appid}&cc=cn&l=english')
    packages = []
    for group in app.get(str(appid), {}).get('data', {}).get('package_groups', []):
        for sub in group.get('subs', []):
            subid = sub.get('packageid')
            query = urllib.parse.urlencode({'subId': subid, 'appId': appid, 'type': 'subid'})
            plugin = client.get('https://steampy.com/xboot/common/plugIn/getGame?' + query)
            row = {'subId': subid, 'name': sub.get('option_text'), 'plugin': plugin}
            game_id = (plugin.get('result') or {}).get('id')
            if game_id:
                query = urllib.parse.urlencode({'pageNumber': 1, 'pageSize': 20,
                    'sort': 'keyPrice', 'order': 'asc', 'gameId': game_id})
                row['orderbook'] = client.get('https://steampy.com/xboot/steamKeySale/listSale?' + query)
            packages.append(row)
    result = {'appid': appid, 'checkedAt': now_iso(), 'packages': packages}
    if output:
        Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('appid', type=int)
    parser.add_argument('--output')
    args = parser.parse_args()
    print(json.dumps(probe(args.appid, args.output), ensure_ascii=False, indent=2))
