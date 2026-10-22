"""Shared planar boundaries and bounded cubic Bezier fitting in source pixels.

The least-squares cubic construction follows Schneider's Graphics Gems method;
topology guards, corner anchors and sampling are specific to this model pipeline.
https://github.com/erich666/GraphicsGems/blob/master/gems/FitCurves.c
"""
from collections import defaultdict
import math


def contours_intersect(loops):
    """Spatial broad phase; preserve the original float64 segment predicate."""
    import numpy as np
    segments, owners = [], []
    for owner, loop in enumerate(loops):
        for index, (a, b) in enumerate(zip(loop, loop[1:] + loop[:1])):
            segments.append((a, b))
            owners.append((owner, index, len(loop)))
    if not segments:
        return False
    segments = np.array(segments, dtype=np.float64)
    lower, upper = segments.min(axis=1), segments.max(axis=1)
    extent = float((upper.max(axis=0) - lower.min(axis=0)).max())
    cell_size = max(8., extent / math.sqrt(len(segments)))
    buckets = defaultdict(list)

    def cells(index):
        for y in range(math.floor(lower[index, 1]/cell_size), math.floor(upper[index, 1]/cell_size)+1):
            for x in range(math.floor(lower[index, 0]/cell_size), math.floor(upper[index, 0]/cell_size)+1):
                yield x, y

    for index in range(len(segments)):
        for cell in cells(index):
            buckets[cell].append(index)
    for index, (a, b) in enumerate(segments):
        candidates = sorted({other for cell in cells(index) for other in buckets[cell] if other > index})
        if not candidates:
            continue
        candidates = np.array(candidates, dtype=np.intp)
        candidates = candidates[np.all(np.maximum(lower[index], lower[candidates]) <=
                                       np.minimum(upper[index], upper[candidates]), axis=1)]
        if not len(candidates):
            continue
        others = segments[candidates]
        c, d = others[:, 0], others[:, 1]
        ab, cd = b - a, d - c
        cross = lambda u, v: u[..., 0] * v[..., 1] - u[..., 1] * v[..., 0]
        touching = ((cross(ab, c - a) * cross(ab, d - a) <= 0) &
                    (cross(cd, a - c) * cross(cd, b - c) <= 0) &
                    np.all(np.maximum(np.minimum(a, b), np.minimum(c, d)) <=
                           np.minimum(np.maximum(a, b), np.maximum(c, d)), axis=1))
        for offset in np.flatnonzero(touching):
            owner, position, size = owners[index]
            other_owner, other_position, _ = owners[int(candidates[offset])]
            if owner != other_owner or (position - other_position) % size not in (1, size - 1):
                return True
    return False


def path_error(points, reference):
    """Maximum sampled distance to a polyline, with bounded temporary arrays."""
    import numpy as np
    points, reference = np.asarray(points, dtype=float), np.asarray(reference, dtype=float)
    a, direction = reference[:-1], np.diff(reference, axis=0)
    lengths = np.maximum(np.sum(direction * direction, axis=1), 1e-20)
    maximum = 0.
    for start in range(0, len(points), 64):
        delta = points[start:start + 64, None, :] - a[None, :, :]
        t = np.clip(np.sum(delta * direction, axis=2) / lengths, 0, 1)
        distance = np.sum((delta - t[:, :, None] * direction) ** 2, axis=2)
        maximum = max(maximum, float(np.sqrt(distance.min(axis=1)).max()))
    return maximum


def sample_cubic(control, flatness=0.025):
    import numpy as np
    control = np.asarray(control, dtype=float)

    def split(c, depth):
        axis = c[3] - c[0]
        length = float(np.linalg.norm(axis))
        distances = [abs(axis[0] * (p[1] - c[0, 1]) - axis[1] * (p[0] - c[0, 0])) / max(length, 1e-12)
                     for p in c[1:3]]
        # Also reject controls folding past either endpoint on a straight line.
        projections = [float(np.dot(p - c[0], axis)) for p in c[1:3]]
        if depth >= 16 or (max(distances) <= flatness and all(0 <= t <= length * length for t in projections)):
            return [c[0].tolist(), c[3].tolist()]
        a, b, d = (c[0] + c[1]) / 2, (c[1] + c[2]) / 2, (c[2] + c[3]) / 2
        e, f = (a + b) / 2, (b + d) / 2
        middle = (e + f) / 2
        return split(np.array([c[0], a, e, middle]), depth + 1)[:-1] + split(np.array([middle, f, d, c[3]]), depth + 1)

    return split(control, 0)


def regularize_path(points, closed, smoothing_scale=1.):
    """Filter stair steps at uniform arc length, retaining corners and junctions."""
    import numpy as np
    p = np.asarray(points, dtype=float)
    if closed and np.array_equal(p[0], p[-1]):
        p = p[:-1]
    if len(p) < 8:
        return p.tolist()
    anchors = []
    for i in range(len(p)):
        if not closed and (i < 6 or i + 6 >= len(p)):
            continue
        a = np.array([p[j % len(p)] for j in range(i - 6, i + 1)])
        b = np.array([p[j % len(p)] for j in range(i, i + 7)])
        u, v = a[-1] - a[0], b[-1] - b[0]
        lengths = np.linalg.norm(u), np.linalg.norm(v)
        if min(lengths) < 1e-9:
            continue
        straight = min(lengths[0] / np.linalg.norm(np.diff(a, axis=0), axis=1).sum(),
                       lengths[1] / np.linalg.norm(np.diff(b, axis=0), axis=1).sum())
        if straight > .98 and np.dot(u, v) / math.prod(lengths) < math.cos(math.radians(55)):
            anchors.append(i)
    if not closed:
        anchors = sorted(set([0, len(p) - 1] + anchors))
    if not anchors:
        spans = [(0, len(p))]
    else:
        spans = list(zip(anchors, anchors[1:] + ([anchors[0] + len(p)] if closed else [])))
    output = []
    for start, end in spans:
        data = np.array([p[i % len(p)] for i in range(start, end + 1)])
        distance = np.r_[0., np.cumsum(np.linalg.norm(np.diff(data, axis=0), axis=1))]
        positions = np.linspace(0, distance[-1], max(2, round(distance[-1] * 2) + 1))
        sampled = np.column_stack([np.interp(positions, distance, data[:, k]) for k in (0, 1)])
        # Small details get a shorter averaging radius than large filled regions.
        sigma = min(2.2, max(.35, float(np.ptp(data, axis=0).min()) * .25)) * smoothing_scale
        step = max(positions[1] - positions[0], 1e-9)
        radius = max(1, math.ceil(3 * sigma / step))
        offsets = np.arange(-radius, radius + 1)
        weights = np.exp(-.5 * (offsets * step / sigma) ** 2)
        weights /= weights.sum()
        periodic = closed and not anchors
        base = sampled[:-1] if periodic else sampled
        padded = np.pad(base, ((radius, radius), (0, 0)), mode='wrap' if periodic else 'edge')
        smooth = np.column_stack([np.convolve(padded[:, k], weights, mode='valid') for k in (0, 1)])
        if periodic:
            smooth = np.vstack([smooth, smooth[0]])
        else:
            fade = np.minimum(1., np.minimum(positions, distance[-1] - positions) / max(sigma * 2, 1e-9))
            smooth = sampled + (smooth - sampled) * fade[:, None]
        output.extend(smooth.tolist() if not output else smooth[1:].tolist())
    return output


def fit_path(points, closed=False, tolerance=0.25, smooth=True, smoothing_scale=1.):
    """Fit a path with fixed junctions, extrema and sustained sharp corners."""
    import numpy as np
    original = [list(map(float, point)) for point in points]
    if closed and original[0] != original[-1]:
        original.append(original[0])
    if smooth:
        points = regularize_path(points, closed, smoothing_scale)
    cleaned = [list(map(float, points[0]))]
    for point in points[1:]:
        if list(point) != cleaned[-1]:
            cleaned.append(list(map(float, point)))
    if closed and cleaned[-1] == cleaned[0]:
        cleaned.pop()
    p = np.asarray(cleaned, dtype=float)
    count = len(p)
    corners = set()
    span = min(3, max(1, count // 4))
    for i in range(count):
        if not closed and (i < span or i + span >= count):
            continue
        before = np.array([p[j % count] for j in range(i - span, i + 1)])
        after = np.array([p[j % count] for j in range(i, i + span + 1)])
        incoming, outgoing = before[-1] - before[0], after[-1] - after[0]
        lengths = [float(np.linalg.norm(v)) for v in (incoming, outgoing)]
        if min(lengths) < 1e-10:
            continue
        straight = [lengths[k] / max(float(np.linalg.norm(np.diff(part, axis=0), axis=1).sum()), 1e-10)
                    for k, part in enumerate((before, after))]
        if min(straight) > 0.98 and float(np.dot(incoming, outgoing)) / math.prod(lengths) < math.cos(math.radians(55)):
            corners.add(i)
    if closed:
        anchors = sorted(corners | {int(np.argmin(p[:, 0])), int(np.argmax(p[:, 0])),
                                    int(np.argmin(p[:, 1])), int(np.argmax(p[:, 1]))})
        if len(anchors) < 2:
            anchors = [0, count // 2]
        spans = [(a, b if b > a else b + count) for a, b in zip(anchors, anchors[1:] + anchors[:1])]
    else:
        anchors = sorted(corners | {0, count - 1})
        spans = list(zip(anchors, anchors[1:]))
    curves, maximum, linear_fallbacks = [], 0., 0

    def unit(vector):
        return vector / max(float(np.linalg.norm(vector)), 1e-12)

    def fit(data, left, right, depth=0):
        nonlocal maximum, linear_fallbacks
        chord = float(np.linalg.norm(data[-1] - data[0]))
        if len(data) == 2:
            delta = (data[-1] - data[0]) / 3
            curves.append(np.array([data[0], data[0] + delta, data[-1] - delta, data[-1]]).tolist())
            return
        distance = np.r_[0., np.cumsum(np.linalg.norm(np.diff(data, axis=0), axis=1))]
        u = distance / max(distance[-1], 1e-12)
        for iteration in range(5):
            b0, b1, b2, b3 = (1 - u) ** 3, 3 * u * (1 - u) ** 2, 3 * u * u * (1 - u), u ** 3
            residual = data - (b0 + b1)[:, None] * data[0] - (b2 + b3)[:, None] * data[-1]
            matrix = np.array([[np.dot(b1, b1), np.dot(b1, b2) * np.dot(left, right)],
                               [np.dot(b1, b2) * np.dot(left, right), np.dot(b2, b2)]])
            rhs = np.array([np.dot(b1, residual @ left), np.dot(b2, residual @ right)])
            alpha = np.linalg.solve(matrix, rhs) if abs(float(np.linalg.det(matrix))) > 1e-12 else np.array([chord / 3] * 2)
            if min(alpha) < chord * 1e-6 or max(alpha) > chord * 2:
                alpha[:] = chord / 3
            control = np.array([data[0], data[0] + alpha[0] * left, data[-1] + alpha[1] * right, data[-1]])
            fitted = b0[:, None] * control[0] + b1[:, None] * control[1] + b2[:, None] * control[2] + b3[:, None] * control[3]
            errors = np.linalg.norm(fitted - data, axis=1)
            if float(errors.max()) <= tolerance * 3:
                sampled = np.array(sample_cubic(control))
                dense = np.vstack((sampled, (sampled[:-1] + sampled[1:]) / 2))
                source = np.vstack((data, (data[:-1] + data[1:]) / 2))
                error = max(path_error(dense, data), path_error(source, sampled))
                if error <= tolerance - 0.001:
                    maximum = max(maximum, error)
                    curves.append(control.tolist())
                    return
            else:
                break
            first = 3 * ((1 - u) ** 2)[:, None] * (control[1] - control[0]) + 6 * (u * (1 - u))[:, None] * (control[2] - control[1]) + 3 * (u ** 2)[:, None] * (control[3] - control[2])
            second = 6 * (1 - u)[:, None] * (control[2] - 2 * control[1] + control[0]) + 6 * u[:, None] * (control[3] - 2 * control[2] + control[1])
            delta = fitted - data
            denominator = np.sum(first * first + delta * second, axis=1)
            updated = np.clip(u - np.divide(np.sum(delta * first, axis=1), denominator,
                                           out=np.zeros_like(u), where=abs(denominator) > 1e-12), 0, 1)
            if np.any(np.diff(updated) <= 0):
                break
            u = updated
        if depth >= 20:
            linear_fallbacks += len(data) - 1
            for a, b in zip(data, data[1:]):
                fit(np.array([a, b]), unit(b - a), unit(a - b))
            return
        index = min(len(data) - 2, max(1, int(np.argmax(errors))))
        tangent = unit(data[max(0, index - 3)] - data[min(len(data) - 1, index + 3)])
        fit(data[:index + 1], left, tangent, depth + 1)
        fit(data[index:], -tangent, right, depth + 1)

    for a, b in spans:
        data = np.array([p[i % count] for i in range(a, b + 1)])
        left = unit(data[min(2, len(data) - 1)] - data[0])
        right = unit(data[max(0, len(data) - 3)] - data[-1])
        if a % count not in corners and (closed or a > 0):
            left = unit(p[(a + 2) % count] - p[(a - 2) % count])
        if b % count not in corners and (closed or b < count - 1):
            right = unit(p[(b - 2) % count] - p[(b + 2) % count])
        fit(data, left, right)
    sampled = []
    for curve in curves:
        values = sample_cubic(curve)
        sampled.extend(values if not sampled else values[1:])
    raw = original
    maximum = max(path_error(sampled, raw), path_error(raw, sampled))
    if smooth and maximum > 1.5 * smoothing_scale:
        fallback = fit_path(original, closed, tolerance=min(.35, tolerance), smooth=False)
        fallback['smoothing_fallback'] = True
        return fallback
    return {'points': sampled, 'raw_points': raw, 'curves': curves, 'closed': closed,
            'max_error_px': maximum, 'corner_count': len(corners), 'linear_fallbacks': linear_fallbacks,
            'smoothing_fallback': False}


def _cross(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _intersects(a, b, c, d):
    if any(max(min(a[k], b[k]), min(c[k], d[k])) > min(max(a[k], b[k]), max(c[k], d[k])) + 1e-9 for k in (0, 1)):
        return False
    values = (_cross(a, b, c), _cross(a, b, d), _cross(c, d, a), _cross(c, d, b))
    if values[0] * values[1] > 1e-16 or values[2] * values[3] > 1e-16:
        return False
    shared = any(math.dist(p, q) < 1e-8 for p in (a, b) for q in (c, d))
    if shared:
        if max(map(abs, values)) > 1e-8:
            return False
        axis = 0 if abs(a[0] - b[0]) >= abs(a[1] - b[1]) else 1
        return min(max(a[axis], b[axis]), max(c[axis], d[axis])) - max(min(a[axis], b[axis]), min(c[axis], d[axis])) > 1e-8
    return True


def guard_paths(paths):
    """Keep the raw path locally if fitting introduces a crossing."""
    buckets = defaultdict(list)

    def cells(a, b):
        for y in range(math.floor(min(a[1], b[1]) / 8), math.floor(max(a[1], b[1]) / 8) + 1):
            for x in range(math.floor(min(a[0], b[0]) / 8), math.floor(max(a[0], b[0]) / 8) + 1):
                yield x, y

    def add(index, points):
        for a, b in zip(points, points[1:]):
            for cell in cells(a, b):
                buckets[cell].append((index, a, b))

    for index, path in enumerate(paths):
        add(index, path['raw_points'])
    for index, path in enumerate(paths):
        own = defaultdict(list)
        crossing = False
        for a, b in zip(path['points'], path['points'][1:]):
            for cell in cells(a, b):
                if any(other != index and _intersects(a, b, c, d) for other, c, d in buckets[cell]):
                    crossing = True
                    break
                if any(_intersects(a, b, c, d) for c, d in own[cell]):
                    crossing = True
                    break
            if crossing:
                break
            for cell in cells(a, b):
                own[cell].append((a, b))
        path['intersection_fallback'] = crossing
        if crossing:
            path['points'] = path['raw_points']
            path['curves'] = []
            path['max_error_px'] = 0.
        add(index, path['points'])
    return paths


def color_boundaries(labels):
    """Trace each shared interface once; junctions are fixed path endpoints."""
    import numpy as np
    labels = np.asarray(labels)
    edges, adjacent = [], defaultdict(list)

    def edge(a, b, left, right):
        index = len(edges)
        edges.append((a, b, (int(left), int(right))))
        adjacent[a].append(index)
        adjacent[b].append(index)

    ys, xs = np.where(labels[:, :-1] != labels[:, 1:])
    for y, x in zip(ys.tolist(), xs.tolist()):
        edge((x + 1, y), (x + 1, y + 1), labels[y, x], labels[y, x + 1])
    ys, xs = np.where(labels[:-1] != labels[1:])
    for y, x in zip(ys.tolist(), xs.tolist()):
        edge((x, y + 1), (x + 1, y + 1), labels[y + 1, x], labels[y, x])
    remaining, paths = set(range(len(edges))), []

    def trace(start, first):
        current, pending = start, first
        points, colors = [list(start)], None
        while pending in remaining:
            remaining.remove(pending)
            a, b, sides = edges[pending]
            following = b if current == a else a
            oriented = sides if current == a else tuple(reversed(sides))
            colors = colors or oriented
            if oriented != colors:
                raise ValueError('Color boundary orientation changes inside a shared path.')
            points.append(list(following))
            if following == start or len(adjacent[following]) != 2:
                break
            next_edge = next(i for i in adjacent[following] if i != pending)
            if set(edges[next_edge][2]) != set(colors):
                break
            current, pending = following, next_edge
        paths.append({'raw_points': points, 'colors': colors, 'closed': points[0] == points[-1]})

    for point, indices in adjacent.items():
        if len(indices) != 2 or len({tuple(sorted(edges[i][2])) for i in indices}) != 1:
            for index in indices:
                if index in remaining:
                    trace(point, index)
    while remaining:
        index = min(remaining)
        trace(edges[index][0], index)
    return paths


def boundary_network(loops, color_paths):
    """Split and clip raw interfaces together, so outline contacts are fixed nodes."""
    import cv2
    import numpy as np
    segments, cuts, buckets = [], [], defaultdict(list)
    for owner, loop in enumerate(loops):
        for a, b in zip(loop, loop[1:] + loop[:1]):
            segments.append((tuple(a), tuple(b), ('outline', owner)))
    for path in color_paths:
        for a, b in zip(path['raw_points'], path['raw_points'][1:]):
            segments.append((tuple(a), tuple(b), ('color', *path['colors'])))
    # Clipping/frame extension can map adjacent samples onto the same point.
    # Such a segment has no constraint and cannot be parameterized for splitting.
    segments = [(a, b, tag) for a, b, tag in segments if math.dist(a, b) > 1e-9]

    def cells(a, b):
        for y in range(math.floor(min(a[1], b[1]) / 2), math.floor(max(a[1], b[1]) / 2) + 1):
            for x in range(math.floor(min(a[0], b[0]) / 2), math.floor(max(a[0], b[0]) / 2) + 1):
                yield x, y

    def cross(a, b):
        return a[0] * b[1] - a[1] * b[0]

    for i, (a, b, _) in enumerate(segments):
        cuts.append({0., 1.})
        nearby = set(j for cell in cells(a, b) for j in buckets[cell])
        u = (b[0] - a[0], b[1] - a[1])
        for j in nearby:
            c, d, _ = segments[j]
            v = (d[0] - c[0], d[1] - c[1])
            delta = (c[0] - a[0], c[1] - a[1])
            denominator = cross(u, v)
            if abs(denominator) > 1e-12:
                t, s = cross(delta, v) / denominator, cross(delta, u) / denominator
                if -1e-9 <= t <= 1 + 1e-9 and -1e-9 <= s <= 1 + 1e-9:
                    cuts[i].add(min(1., max(0., t)))
                    cuts[j].add(min(1., max(0., s)))
            elif abs(cross(delta, u)) < 1e-9:
                for point in (c, d):
                    t = sum((point[k] - a[k]) * u[k] for k in (0, 1)) / sum(x * x for x in u)
                    if 0 <= t <= 1:
                        cuts[i].add(t)
                for point in (a, b):
                    s = sum((point[k] - c[k]) * v[k] for k in (0, 1)) / sum(x * x for x in v)
                    if 0 <= s <= 1:
                        cuts[j].add(s)
        for cell in cells(a, b):
            buckets[cell].append(i)
    prepared = [(loop, np.array(loop, dtype=np.float32),
                 max(1., max(abs(v) for p in loop for v in p))*4*np.finfo(np.float32).eps)
                for loop in loops]

    def location(loop, polygon, budget, point):
        # Float32 polygon tests can collapse a narrow interior strip onto its
        # outline and drop only part of a color interface after intersections.
        # A distance safely beyond coordinate rounding needs no slow edge scan.
        distance = cv2.pointPolygonTest(polygon, point, True)
        if abs(distance) > budget:
            return 1 if distance > 0 else -1
        x, y = point
        inside = False
        for a, b in zip(loop, loop[1:] + loop[:1]):
            dx, dy = b[0] - a[0], b[1] - a[1]
            length = math.hypot(dx, dy)
            if length > 0 and abs((x-a[0])*dy-(y-a[1])*dx) <= 1e-9*length:
                if all(min(a[k], b[k])-1e-9 <= point[k] <= max(a[k], b[k])+1e-9 for k in (0, 1)):
                    return 0
            if (a[1] > y) != (b[1] > y) and x < a[0]+(y-a[1])*dx/dy:
                inside = not inside
        return 1 if inside else -1
    edges, adjacent = [], defaultdict(list)
    for (a, b, tag), values in zip(segments, cuts):
        values = sorted(values)
        for t, s in zip(values, values[1:]):
            first = tuple(round(a[k] + t * (b[k] - a[k]), 9) for k in (0, 1))
            last = tuple(round(a[k] + s * (b[k] - a[k]), 9) for k in (0, 1))
            if first == last:
                continue
            if tag[0] == 'color':
                middle = tuple((first[k] + last[k]) / 2 for k in (0, 1))
                hits = [location(loop, polygon, budget, middle) for loop, polygon, budget in prepared]
                if 0 in hits or sum(value > 0 for value in hits) % 2 == 0:
                    continue
            index = len(edges)
            edges.append((first, last, tag))
            adjacent[first].append(index)
            adjacent[last].append(index)
    remaining, paths = set(range(len(edges))), []

    def trace(start, index):
        current, points, tag = start, [list(start)], edges[index][2]
        colors = None
        while index in remaining:
            remaining.remove(index)
            a, b, info = edges[index]
            following = b if current == a else a
            if tag[0] == 'color':
                sides = info[1:] if current == a else info[1:][::-1]
                if colors is not None and sides != colors:
                    raise ValueError('Shared interface orientation is inconsistent.')
                colors = sides
            points.append(list(following))
            if following == start or len(adjacent[following]) != 2:
                break
            other = next(j for j in adjacent[following] if j != index)
            if edges[other][2] != tag:
                break
            current, index = following, other
        path = dict(raw_points=points, closed=points[0] == points[-1], kind=tag[0])
        if tag[0] == 'outline':
            path['loop_index'] = tag[1]
        else:
            path['colors'] = colors
        paths.append(path)

    for point, linked in adjacent.items():
        if len(linked) != 2 or edges[linked[0]][2] != edges[linked[1]][2]:
            for index in linked:
                if index in remaining:
                    trace(point, index)
    while remaining:
        index = min(remaining)
        trace(edges[index][0], index)
    return paths


def network_outlines(paths, count):
    """Join fitted outline arcs through their original shared endpoints."""
    loops = []
    for owner in range(count):
        parts = [path['points'] for path in paths if path['kind'] == 'outline' and path['loop_index'] == owner]
        points = list(parts.pop(0))
        while parts:
            match = next((i for i, part in enumerate(parts) if part[0] == points[-1] or part[-1] == points[-1]), None)
            if match is None:
                raise ValueError('Fitted outline network is disconnected.')
            part = parts.pop(match)
            points.extend((part if part[0] == points[-1] else part[::-1])[1:])
        if points[0] != points[-1]:
            raise ValueError('Fitted outline network is open.')
        loops.append(points[:-1])
    return loops
