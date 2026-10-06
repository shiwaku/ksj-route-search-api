"""経路の違いはアルゴリズムによるものか、の切り分け（docs/routing-engines.md 8 章）

ビューワの /api/batch と同じ 100 組（seed=1）で、
  1. 同じエンジン・同じ重みでアルゴリズムだけ替える（GraphHopper CH ↔ ダイクストラ法、OSRM MLD ↔ CH）
  2. Valhalla と OSRM で経路が違う組について、OSRM の経路を Valhalla の物差し（/trace_route）で測り直す
  3. Valhalla の階層の打ち切り（hierarchy_limits）を外して 2 をやり直す
を数える。

前提: bash bench/compare/build.sh 済みで 3 エンジンが起動している。加えて、比較用に 2 つ立てる
    uv run python -m bench.compare.algo_test --make-config      # → bench/compare/data/valhalla_nolimit.json
    cd bench/compare && export MSYS_NO_PATHCONV=1
    docker compose run --rm --no-deps osrm osrm-contract /data/roads.osrm
    docker run -d --name compare-osrm-ch -p 5001:5000 -v "$PWD/data:/data" ghcr.io/project-osrm/osrm-backend osrm-routed --algorithm ch /data/roads.osrm
    docker run -d --name compare-valhalla-nolimit -p 8003:8002 -v "$PWD/data:/data" ghcr.io/valhalla/valhalla valhalla_service /data/valhalla_nolimit.json 2
    （Windows の Git Bash では $PWD の代わりに $(pwd -W)）
実行:
    uv run python -m bench.compare.algo_test
"""
import json
import random
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
OSRM_CH = 'http://localhost:5001'
VALHALLA_NOLIMIT = 'http://localhost:8003'


def make_config():
    """bench/valhalla/valhalla.json の hierarchy_limits を全部外した設定を data/ に書く"""
    c = json.loads((HERE.parent / 'valhalla' / 'valhalla.json').read_text(encoding='utf-8'))

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k == 'hierarchy_limits':
                    v['max_up_transitions'] = {'1': 1_000_000, '2': 1_000_000}
                    v['expand_within_distance'] = {'0': 1e8, '1': 1e8, '2': 1e8}
                else:
                    walk(v)
    walk(c)
    out = HERE / 'data' / 'valhalla_nolimit.json'
    out.write_text(json.dumps(c, indent=1), encoding='utf-8')
    print(f'→ {out}')


if '--make-config' in sys.argv:
    make_config()
    sys.exit()

from bench.compare import server as s      # noqa: E402  グラフを読むので --make-config のときは読まない

g = s.g
rng = random.Random(1)
pairs = []
for _ in range(100):
    i, j = rng.randrange(g.n_snap_nodes), rng.randrange(g.n_snap_nodes)
    pairs.append(((float(g.kd_lat[i]), float(g.kd_lon[i])), (float(g.kd_lat[j]), float(g.kd_lon[j]))))


def osrm(url, a, b):
    r = s._get(f'{url}/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}?overview=full&geometries=geojson')['routes'][0]
    return r['geometry']['coordinates']


def gh(a, b, extra=''):
    p = s._get(f'{s.GH}/route?point={a[0]},{a[1]}&point={b[0]},{b[1]}&profile=car&points_encoded=false'
               f'&instructions=false&way_point_max_distance=0{extra}')['paths'][0]
    return p['points']['coordinates']


def val_route(url, a, b):
    r = s._get(f'{url}/route', {'locations': [{'lat': a[0], 'lon': a[1]}, {'lat': b[0], 'lon': b[1]}],
                                'costing': 'auto', 'directions_type': 'none'})
    leg = r['trip']['legs'][0]
    return s._decode_polyline6(leg['shape']), leg['summary']['time']


def val_trace(url, coords):
    """経路の形を Valhalla の道路網上でなぞり、Valhalla の物差しでの所要（秒）を返す"""
    r = s._get(f'{url}/trace_route', {'shape': [{'lat': y, 'lon': x} for x, y in coords], 'costing': 'auto',
                                      'shape_match': 'map_snap', 'directions_type': 'none'})
    return sum(leg['summary']['time'] for leg in r['trip']['legs'])


def links(coords):
    return s.to_links(coords)[0]


def same_weights(name, f1, f2):
    """2 つの経路をリンク列と KSJ 換算の所要で比べる"""
    same = tie = other = 0
    dmax = 0.0
    for a, b in pairs:
        l1, l2 = links(f1(a, b)), links(f2(a, b))
        d = abs(int(g.link_time[l1].sum()) - int(g.link_time[l2].sum())) * 0.6       # 0.01 分 → 秒
        if l1 == l2:
            same += 1
        elif d < 0.3:
            tie += 1
        else:
            other += 1
            dmax = max(dmax, d)
    print(f'{name}: 同じ {same}・違う・所要同じ {tie}・違う・所要も違う {other}・最大差 {dmax:.1f} 秒')


def valhalla_vs_osrm(url, label):
    """Valhalla と OSRM で経路が違う組で、OSRM の経路を Valhalla の物差しで測る。
    なぞりの誤差の目安として、Valhalla 自身の経路をなぞった所要と /route の所要の差も出す"""
    self_err, d = [], []
    for a, b in pairs:
        vc, vt = val_route(url, a, b)
        oc = osrm(s.OSRM, a, b)
        t_self = val_trace(url, vc)
        self_err.append(abs(t_self - vt))
        if links(vc) != links(oc):
            d.append(val_trace(url, oc) - t_self)
    d = np.array(d)
    print(f'{label}: なぞりの誤差 最大 {max(self_err):.1f} 秒。OSRM と経路が違う {len(d)} 組のうち、'
          f'Valhalla の物差しで OSRM の経路の方が速い {(d < -0.5).sum()} 組（3 秒超 {(d < -3).sum()} 組・最大 {-d.min():.0f} 秒）')
    return d


same_weights('GraphHopper CH ↔ ダイクストラ法', gh, lambda a, b: gh(a, b, '&ch.disable=true&algorithm=dijkstra'))
same_weights('OSRM MLD ↔ CH', lambda a, b: osrm(s.OSRM, a, b), lambda a, b: osrm(OSRM_CH, a, b))
same_weights('OSRM MLD ↔ GraphHopper CH（参考）', lambda a, b: osrm(s.OSRM, a, b), gh)
valhalla_vs_osrm(s.VALHALLA, 'Valhalla（既定の打ち切りあり）')
changed, gain = 0, []
for a, b in pairs:
    c0, t0 = val_route(s.VALHALLA, a, b)
    c1, t1 = val_route(VALHALLA_NOLIMIT, a, b)
    changed += links(c0) != links(c1)
    gain.append(t0 - t1)
print(f'打ち切りを外して経路が変わった {changed} 組・所要の短縮 最大 {max(gain):.0f} 秒')
valhalla_vs_osrm(VALHALLA_NOLIMIT, 'Valhalla（打ち切りなし）')
