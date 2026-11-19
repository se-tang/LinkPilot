#!/usr/bin/env python3
"""生成 app/static/world-110m.json：全球视图底图。

面板 CSP 限制 script-src 'self'，前端不能加载 d3 / topojson-client，
所以底图在这里做三件事后存成纯 JSON：
  1. 解码 world-atlas 的 TopoJSON（arcs 差分编码），得到陆地带多边形；
  2. 用 Natural Earth I 投影把经纬度变成单位坐标（x ∈ ±2.7354, y ∈ ±1.4227）；
  3. 量化成整数并输出坐标数组，前端只做线性缩放，不再需要任何投影代码。

输入（Natural Earth 数据，公有领域）：
  curl -fsSLo countries-110m.json \
    https://cdn.jsdelivr.net/npm/world-atlas@2/countries-110m.json
  python tools/gen_world_map.py countries-110m.json
"""
import json
import math
import sys


def natural_earth_1(lon, lat):
    """与 d3-geo 相同的 Natural Earth I 多项式，输入度，输出单位坐标。"""
    lam = math.radians(lon)
    phi = math.radians(lat)
    p2 = phi * phi
    p4 = p2 * p2
    x = lam * (0.8707 - 0.131979 * p2 + p4 * (-0.013791 + p4 * (0.003971 * p2 - 0.001529 * p4)))
    y = phi * (1.007226 + p2 * (0.015085 + p4 * (-0.044475 + 0.028874 * p2 - 0.005916 * p4)))
    return x, y


def decode_arcs(topo):
    """TopoJSON arcs：量化差分编码还原成绝对坐标（lon, lat）。"""
    scale = topo['transform']['scale']
    translate = topo['transform']['translate']
    arcs = []
    for arc in topo['arcs']:
        x = y = 0
        ring = []
        for dx, dy in arc:
            x += dx
            y += dy
            ring.append((x * scale[0] + translate[0], y * scale[1] + translate[1]))
        arcs.append(ring)
    return arcs


def ring_coords(arcs, ring_idxs):
    """把一条 ring 的 arc 索引序列拼成完整坐标环（负索引 = 反向弧）。"""
    pts = []
    for idx in ring_idxs:
        arc = arcs[idx] if idx >= 0 else arcs[~idx][::-1]
        if pts and pts[-1] == arc[0]:
            pts.extend(arc[1:])
        else:
            pts.extend(arc)
    return pts


def collect_rings(geom, arcs, out):
    gtype = geom.get('type')
    if gtype == 'Polygon':
        out.extend(ring_coords(arcs, r) for r in geom['arcs'])
    elif gtype == 'MultiPolygon':
        for poly in geom['arcs']:
            out.extend(ring_coords(arcs, r) for r in poly)
    elif gtype == 'GeometryCollection':
        for g in geom['geometries']:
            collect_rings(g, arcs, out)


def split_antimeridian(pts):
    """跨 ±180 的环在投影空间会出现横向跳变：拆成若干条开放路径。

    返回 [段坐标列表]；未拆分的环返回单个闭合段（首尾相同）。
    """
    proj = [natural_earth_1(lon, lat) for lon, lat in pts]
    segs, cur = [], [proj[0]]
    for prev, cur_p in zip(proj, proj[1:]):
        if abs(cur_p[0] - prev[0]) > 1.4:      # 穿越了裁剪线
            segs.append(cur)
            cur = [cur_p]
        else:
            cur.append(cur_p)
    segs.append(cur)
    # 段太短（<4 点）填不出面，丢掉
    return [s for s in segs if len(s) >= 4]


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    topo = json.load(open(sys.argv[1], encoding='utf-8'))
    arcs = decode_arcs(topo)
    rings = []
    for geom in topo['objects']['countries']['geometries']:
        collect_rings(geom, arcs, rings)
    polys = []
    dropped = 0
    for ring in rings:
        for seg in split_antimeridian(ring):
            flat = []
            for x, y in seg:
                flat.append(int(round(x * 1000)))
                flat.append(int(round(y * 1000)))
            if len(flat) < 8:
                dropped += 1
                continue
            polys.append(flat)
    pts = sum(len(p) // 2 for p in polys)
    data = {'polys': polys}
    out = 'app/static/world-110m.json'
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(data, f, separators=(',', ':'))
    size = len(json.dumps(data, separators=(',', ':')).encode('utf-8'))
    print(f'{out}: {len(polys)} 段路径, {pts} 个点, {size / 1024:.0f} KB, 丢弃碎块 {dropped}')


if __name__ == '__main__':
    main()
