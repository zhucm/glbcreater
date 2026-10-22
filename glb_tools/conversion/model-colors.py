"""Classify flat-color PNG artwork and construct a texture-free color partition."""
import os
import importlib.util
from pathlib import Path

curve_spec = importlib.util.spec_from_file_location('model_curves', Path(__file__).with_name('model-curves.py'))
curves = importlib.util.module_from_spec(curve_spec)
curve_spec.loader.exec_module(curves)


class SkippedInput(ValueError):
    def __init__(self, reason, message, metrics=None):
        super().__init__(message)
        self.reason = reason
        self.metrics = metrics or {}


MAX_GEOMETRY_DIMENSION = 256
MAX_STABLE_COLORS = 24
FRINGE_BUDGET = 0.35
NOISE_BUDGET = 0.08
MIN_STABLE_REGION_AREA = 16
VERTEX_MAX_FOREGROUND = 200_000
VERTEX_RECONSTRUCTION_ERROR_LIMIT = 48
VERTEX_RECONSTRUCTION_OVER_LIMIT = 0.3
COLOR_REJECT_REASONS = ('too-many-colors', 'gradient-or-texture', 'fragmented-colors', 'complex-colors')


def repair_diagonal_contacts(labels):
    """Fill point-only contacts until a full pass makes no changes."""
    filled = 0
    while True:
        changed = 0
        for y in range(len(labels) - 1):
            for x in range(len(labels[0]) - 1):
                top_left, top_right = labels[y][x], labels[y][x + 1]
                bottom_left, bottom_right = labels[y + 1][x], labels[y + 1][x + 1]
                if top_left >= 0 and bottom_right >= 0 and top_right < 0 and bottom_left < 0:
                    labels[y][x + 1] = top_left
                    changed += 1
                elif top_right >= 0 and bottom_left >= 0 and top_left < 0 and bottom_right < 0:
                    labels[y][x] = top_right
                    changed += 1
        filled += changed
        if not changed:
            return filled


def _foreground(rgba):
    """Shared foreground rule: alpha threshold or a border-connected white backdrop."""
    import cv2
    import numpy as np

    rgb, alpha = rgba[:, :, :3], rgba[:, :, 3]
    alpha_threshold = max(1, int(np.ceil(int(alpha.max()) * 0.90)))
    mask = alpha >= alpha_threshold
    background = 'alpha'
    if not np.any(alpha < 128):
        white = (rgb.min(2) >= 245) & (np.ptp(rgb, axis=2) <= 10)
        border = np.concatenate((white[0], white[-1], white[:, 0], white[:, -1]))
        if border.mean() < 0.95:
            raise SkippedInput('ambiguous-background', 'No transparency or reliably white outer border.')
        _, components = cv2.connectedComponents(white.astype('uint8'), connectivity=4)
        touching = np.unique(np.concatenate((components[0], components[-1], components[:, 0], components[:, -1])))
        mask = ~np.isin(components, touching[touching != 0])
        background = 'border-connected-white'
    return rgb, alpha, mask, background, alpha_threshold


def _analyze_with_fallbacks(path, color=None, recover_cartoon_colors=False):
    try:
        return classify_image(path, color)
    except SkippedInput as initial:
        if color or initial.reason not in COLOR_REJECT_REASONS:
            raise
        for merge_colors in (False, True):
            try:
                result = classify_image(path, color, refine_colors=True, merge_colors=merge_colors)
                break
            except SkippedInput as error:
                refined = error
        else:
            try:
                result = analyze_line_art(path)
            except SkippedInput:
                result = None
            if result is not None:
                result['analysis']['initial_skip_reason'] = initial.reason
                return result
            recovery_error = None
            if recover_cartoon_colors:
                try:
                    result = recover_cartoon_palette(path)
                    result['analysis']['initial_skip_reason'] = initial.reason
                    return result
                except SkippedInput as error:
                    recovery_error = error
            try:
                result = analyze_dominant_color(path)
            except SkippedInput as vertex_error:
                vertex_error.metrics['initial_skip_reason'] = initial.reason
                raise
            result['analysis'].update(color_fidelity='dominant-only',
                                      color_warning='Original colors could not be preserved; exported a single dominant-color silhouette.',
                                      refined_skip_reason=refined.reason,
                                      refined_skip_message=str(refined))
            if recovery_error is not None:
                result['analysis'].update(cartoon_recovery_rejected=recovery_error.reason,
                                          cartoon_recovery_message=str(recovery_error),
                                          cartoon_recovery_metrics=recovery_error.metrics)
        result['analysis']['initial_skip_reason'] = initial.reason
        return result


def _downscale_temporary(path, max_foreground=VERTEX_MAX_FOREGROUND):
    """Auto-downscale oversized foregrounds; the color grid is capped at 256."""
    import tempfile

    import numpy as np
    from PIL import Image

    with Image.open(path) as image:
        if image.format != 'PNG':
            raise SkippedInput('unsupported-format', 'Only PNG input is supported.')
        rgba = np.array(image.convert('RGBA'))
    _, _, mask, _, _ = _foreground(rgba)
    count = max(1, int(mask.sum()))
    factor = (max_foreground / count) ** 0.5 * 0.98
    if factor >= 1:
        return path, 1.0
    size = (max(1, round(rgba.shape[1] * factor)), max(1, round(rgba.shape[0] * factor)))
    handle = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
    Image.fromarray(rgba, 'RGBA').resize(size, Image.LANCZOS).save(handle, format='PNG')
    handle.close()
    return handle.name, factor


def analyze_image(path, color=None, recover_cartoon_colors=False):
    try:
        result = _analyze_with_fallbacks(path, color, recover_cartoon_colors)
    except SkippedInput as initial:
        if color or initial.reason != 'excessive-geometry':
            raise
        scaled_path, factor = _downscale_temporary(path)
        try:
            result = _analyze_with_fallbacks(scaled_path, color, recover_cartoon_colors)
        except SkippedInput as scaled_error:
            scaled_error.metrics['initial_skip_reason'] = 'excessive-geometry'
            raise
        finally:
            if scaled_path != path:
                os.unlink(scaled_path)
        result['analysis']['initial_skip_reason'] = 'excessive-geometry'
        result['analysis']['source_downscale_factor'] = round(factor, 6)
    try:
        attach_contours(path, result)
    except (SkippedInput, ValueError) as error:
        if result['analysis'].get('mode') != 'cartoon-recovered-palette':
            raise
        result = analyze_image(path, color)
        result['analysis'].update(cartoon_recovery_rejected='cartoon-boundary-validation',
                                  cartoon_recovery_message=str(error))
    return result


def marching_contours(field, level=0.5):
    """Interpolate pixel-center crossings; ambiguous contacts stay disconnected."""
    import numpy as np

    padded = np.pad(field, 1)
    inside = padded > level
    cases = (inside[:-1, :-1].astype(np.uint8) + inside[:-1, 1:] * 2 +
             inside[1:, 1:] * 4 + inside[1:, :-1] * 8)
    pairs = {1: [(3, 0)], 2: [(0, 1)], 3: [(3, 1)], 4: [(1, 2)],
             5: [(3, 0), (1, 2)], 6: [(0, 2)], 7: [(3, 2)],
             8: [(2, 3)], 9: [(2, 0)], 10: [(0, 1), (2, 3)],
             11: [(2, 1)], 12: [(1, 3)], 13: [(1, 0)], 14: [(0, 3)]}
    points, adjacent = {}, {}
    ys, xs = np.where((cases != 0) & (cases != 15))
    for y, x in zip(ys.tolist(), xs.tolist()):
        corners = ((x, y), (x + 1, y), (x + 1, y + 1), (x, y + 1))
        keys = ((0, x, y), (1, x + 1, y), (0, x, y + 1), (1, x, y))
        for a, b in pairs[int(cases[y, x])]:
            for edge in (a, b):
                key = keys[edge]
                if key not in points:
                    (ax, ay), (bx, by) = corners[edge], corners[(edge + 1) % 4]
                    va, vb = float(padded[ay, ax]), float(padded[by, bx])
                    t = (level - va) / (vb - va)
                    points[key] = (ax + (bx - ax) * t - 0.5, ay + (by - ay) * t - 0.5)
            adjacent.setdefault(keys[a], []).append(keys[b])
            adjacent.setdefault(keys[b], []).append(keys[a])
    loops = []
    remaining = set(points)
    while remaining:
        start = min(remaining)
        loop, previous, current = [], None, start
        while True:
            if current not in remaining or len(adjacent[current]) != 2:
                raise SkippedInput('unsupported-topology', 'Contour is not a closed simple loop.')
            remaining.remove(current)
            loop.append(points[current])
            a, b = adjacent[current]
            following = b if a == previous else a
            previous, current = current, following
            if current == start:
                break
        if len(loop) >= 3:
            loops.append(loop)
    return loops


def remove_edge_matte(labels, palette):
    """Remove unsupported white matte fragments, retaining white fill interiors."""
    import cv2
    import numpy as np
    valid = labels >= 0
    depth = cv2.distanceTransform(np.pad(valid.astype('uint8'), 1), cv2.DIST_L2, 5)[1:-1, 1:-1]
    remove = np.zeros(labels.shape, dtype=bool)
    for index, color in enumerate(palette):
        if min(color) < 220 or max(color) - min(color) > 40:
            continue
        white = np.uint8(labels == index)
        count, parts, _, _ = cv2.connectedComponentsWithStats(white, connectivity=4)
        core = cv2.erode(white, np.ones((3, 3), np.uint8))
        for part in range(1, count):
            region = parts == part
            if depth[region].min() <= 1 and depth[region].max() <= 4.5 and core[region].sum() < 4:
                remove |= region
    nonwhite = valid & ~np.isin(labels, [i for i, c in enumerate(palette) if min(c) >= 220 and max(c) - min(c) <= 40])
    if remove.any() and nonwhite.any():
        _, nearest = cv2.distanceTransformWithLabels(np.uint8(~nonwhite), cv2.DIST_L2, 5,
                                                     labelType=cv2.DIST_LABEL_PIXEL)
        labels[remove] = labels[nonwhite][nearest[remove] - 1]
        return int(remove.sum())
    return 0


def preserve_stroke_labels(rgb, labels, palette, mask, fringe):
    """Retain connected dark antialiased strokes using a local two-color mixture."""
    import cv2
    import numpy as np
    protected = np.zeros(mask.shape, dtype=bool)
    darkest = int(np.argmin(np.mean(palette, axis=1)))
    stroke = np.array(palette[darkest], dtype=np.float32)
    observed = rgb.astype(np.float32)
    seed = mask & (labels == darkest) & (abs(observed - stroke).max(axis=2) <= 24)
    candidate = seed.copy()
    for color in palette:
        fill = np.array(color, dtype=np.float32)
        vector = fill - stroke
        if vector.min() < 0 or vector.max() < 64:
            continue
        coverage = ((fill - observed) * vector).sum(axis=2) / (vector * vector).sum()
        reconstructed = fill - coverage[:, :, None] * vector
        candidate |= mask & fringe & (coverage >= .3) & (coverage <= 1.) & (abs(reconstructed - observed).max(axis=2) <= 12)
    count, parts, stats, _ = cv2.connectedComponentsWithStats(np.uint8(candidate), connectivity=4)
    supported = set(np.unique(parts[seed])) - {0}
    for part in supported:
        region = parts == part
        if stats[part, cv2.CC_STAT_AREA] < 4:
            continue
        labels[region] = darkest
        if min(stats[part, cv2.CC_STAT_WIDTH], stats[part, cv2.CC_STAT_HEIGHT]) <= 3:
            protected |= region
    return protected


def attach_contours(path, result):
    """Keep source-resolution geometry independent of the color grid resolution."""
    import cv2
    import numpy as np
    from PIL import Image

    with Image.open(path) as image:
        rgba = np.array(image.convert('RGBA'))
    _, alpha, mask, background, _ = _foreground(rgba)
    stroke_field = result.pop('_stroke_coverage', None)
    if stroke_field is not None:
        mask = stroke_field > .5
    count, parts, stats, _ = cv2.connectedComponentsWithStats(mask.astype('uint8'), connectivity=4)
    seed = np.isin(parts, [i for i in range(1, count) if stats[i, cv2.CC_STAT_AREA] >= 4])
    field = alpha.astype(np.float32) / max(1, int(alpha.max())) if background == 'alpha' else seed.astype(np.float32)
    if stroke_field is not None:
        field = stroke_field
    # No new translucent islands or shadows without a stable opaque foreground.
    _, coverage_parts = cv2.connectedComponents((field > 0.5).astype('uint8'), connectivity=4)
    supported = np.unique(coverage_parts[seed])
    field[(field > 0.5) & ~np.isin(coverage_parts, supported[supported != 0])] = 0
    raw = marching_contours(field)

    def topology(loops):
        # Test continuous polygons: rasterizing can close subpixel gaps and invent holes.
        polygons = [np.array(loop, dtype=np.float32) for loop in loops]
        depths = [sum(cv2.pointPolygonTest(other, tuple(map(float, polygon[0])), False) > 0
                      for j, other in enumerate(polygons) if i != j) for i, polygon in enumerate(polygons)]
        return sum(depth % 2 == 0 for depth in depths), sum(depth % 2 == 1 for depth in depths)

    original_topology = topology(raw)
    ys, xs = np.where(seed)
    left, top = int(xs.min()), int(ys.min())
    source_width, source_height = int(xs.max()) + 1 - left, int(ys.max()) + 1 - top
    grid_height, grid_width = len(result['labels']), len(result['labels'][0])
    # Supply nearest valid color for triangles in fringes and small source details
    # that the color grid no longer contains; no geometry is added from this map.
    labels = np.array(result['labels'], dtype=np.int16)
    valid = labels >= 0
    if not valid.any():
        raise SkippedInput('unsupported-topology', 'Color grid contains no valid foreground samples.')
    _, nearest = cv2.distanceTransformWithLabels(np.uint8(~valid), cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    labels[~valid] = labels[valid][nearest[~valid] - 1]
    result['surface_labels'] = labels.tolist()
    source_labels = result.pop('_source_labels', None)
    if source_labels is None:
        source_labels = np.array(result['labels'], dtype=np.int16)
    if source_labels.shape != (source_height, source_width):
        source_labels = cv2.resize(source_labels, (source_width, source_height), interpolation=cv2.INTER_NEAREST)
    result['analysis']['removed_edge_matte_pixels'] = (remove_edge_matte(source_labels, result['palette'])
                                                     if background == 'alpha' else 0)
    source_valid = source_labels >= 0
    _, nearest = cv2.distanceTransformWithLabels(np.uint8(~source_valid), cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    source_labels[~source_valid] = source_labels[source_valid][nearest[~source_valid] - 1]
    raw_paths = curves.color_boundaries(source_labels)
    x_min = min(p[0] for loop in raw for p in loop)
    x_max = max(p[0] for loop in raw for p in loop)
    y_min = min(p[1] for loop in raw for p in loop)
    y_max = max(p[1] for loop in raw for p in loop)
    color_paths = []
    for path in raw_paths:
        points = [[x_min if x == 0 else x_max if x == source_width else x + left,
                   y_min if y == 0 else y_max if y == source_height else y + top]
                  for x, y in path['raw_points']]
        color_paths.append(dict(raw_points=points, colors=path['colors']))
    network = curves.boundary_network(raw, color_paths)
    raw_network_loops = curves.network_outlines([dict(path, points=path['raw_points']) for path in network], len(raw))
    paths = []
    for path in network:
        fitted = curves.fit_path(path['raw_points'], closed=path['closed'], tolerance=.08 if stroke_field is not None else .25,
                                 smoothing_scale=.2 if stroke_field is not None else 1.)
        fitted.update({key: value for key, value in path.items() if key != 'raw_points'})
        paths.append(fitted)
    paths = curves.guard_paths(paths)
    loops = curves.network_outlines(paths, len(raw))
    fallback = curves.contours_intersect(loops) or topology(loops) != original_topology
    if fallback:
        for path in paths:
            path.update(points=path['raw_points'], curves=[], max_error_px=0., intersection_fallback=True)
        loops = curves.network_outlines(paths, len(raw))
    outline_paths = [path for path in paths if path['kind'] == 'outline']
    color_paths = [path for path in paths if path['kind'] == 'color']
    contour_arrays = [np.array(loop, dtype=np.float32) for loop in loops]
    max_error = max(abs(cv2.pointPolygonTest(polygon, point, True))
                    for original, polygon in zip(raw, contour_arrays) for point in original)
    result['contours'] = [[[(x - left) * grid_width / source_width,
                            (y - top) * grid_height / source_height] for x, y in loop] for loop in loops]

    def to_grid(point):
        return [(point[0] - left) * grid_width / source_width,
                (point[1] - top) * grid_height / source_height]

    result['raw_contours'] = [[to_grid(p) for p in loop] for loop in raw_network_loops]
    result['color_boundaries'] = [{'points': [to_grid(p) for p in path['points']],
                                    'raw_points': [to_grid(p) for p in path['raw_points']],
                                    'colors': path['colors']} for path in color_paths]
    result['curve_paths'] = paths
    result['silhouette_islands'] = original_topology[0]
    result['analysis'].update(geometry_mode='source-contour', contour_source_resolution=[rgba.shape[1], rgba.shape[0]],
                              contour_tolerance_px=.3 if stroke_field is not None else 1.5, contour_simplification_fallback=fallback,
                              contour_max_error_px=round(max_error, 6),
                              contour_raw_vertices=sum(map(len, raw)), contour_vertices=sum(map(len, loops)),
                              contour_components=original_topology[0], contour_holes=original_topology[1])
    result['analysis'].update(boundary_mode='shared-bezier', boundary_network_mode='joint-clipped', boundary_source_resolution=[source_width, source_height],
                              curve_coordinate_space='source-pixels', curve_path_count=len(paths),
                              bezier_count=sum(len(p['curves']) for p in paths),
                              curve_max_error_px=round(max(p['max_error_px'] for p in paths), 6),
                              curve_intersection_fallbacks=sum(p['intersection_fallback'] for p in paths),
                              curve_smoothing_fallbacks=sum(p['smoothing_fallback'] for p in paths),
                              curve_linear_fallbacks=sum(p['linear_fallbacks'] for p in paths))


def edge_coverage(alpha, mask, background, bounds, size):
    """Keep coverage separate from the conservative foreground classification."""
    import cv2
    import numpy as np

    left, top, right, bottom = bounds
    coverage = alpha.astype(np.float32) / max(1, int(alpha.max())) if background == 'alpha' else mask.astype(np.float32)
    return cv2.resize(coverage[top:bottom, left:right], size, interpolation=cv2.INTER_AREA).tolist()


def dominant_rgb(rgb, mask):
    """Use the most populated coarse RGB bucket, not an average of unrelated fills."""
    import numpy as np

    pixels = rgb[mask].astype(np.uint16)
    if not len(pixels):
        raise SkippedInput('empty-image', 'No visible foreground for dominant color.')
    bins = pixels // 16
    keys = bins[:, 0] * 256 + bins[:, 1] * 16 + bins[:, 2]
    winner = int(np.bincount(keys, minlength=4096).argmax())
    return np.rint(pixels[keys == winner].mean(axis=0)).astype(int).tolist()


def analyze_line_art(path):
    """Recover narrow opaque strokes from a validated white-matte color mixture."""
    import cv2
    import numpy as np
    from PIL import Image
    with Image.open(path) as image:
        rgba = np.array(image.convert('RGBA'))
    rgb, alpha, mask, background, threshold = _foreground(rgba)
    dark = mask & (rgb.max(axis=2) < 80) & (np.ptp(rgb, axis=2) < 16)
    if dark.sum() < 16:
        raise SkippedInput('not-line-art', 'No stable dark stroke support.')
    values, counts = np.unique(rgb[dark], axis=0, return_counts=True)
    palette = [values[counts.argmax()].tolist()]
    chromatic = mask & (np.ptp(rgb, axis=2) > 40)
    count, parts, stats, _ = cv2.connectedComponentsWithStats(np.uint8(chromatic), connectivity=4)
    for part in range(1, count):
        if stats[part, cv2.CC_STAT_AREA] < 4:
            continue
        values, counts = np.unique(rgb[parts == part], axis=0, return_counts=True)
        color = values[counts.argmax()]
        if min(np.max(abs(np.array(c) - color.astype(int))) for c in palette) > 32:
            palette.append(color.tolist())
        if len(palette) > 8:
            raise SkippedInput('not-line-art', 'Too many independent stroke colors.')
    observed = rgb.astype(np.float32)
    errors, coverages = [], []
    for color in palette:
        direction = 255. - np.array(color, dtype=np.float32)
        coverage = np.clip(((255. - observed) * direction).sum(axis=2) / (direction * direction).sum(), 0, 1)
        reconstructed = 255. - coverage[:, :, None] * direction
        errors.append(abs(reconstructed - observed).max(axis=2))
        coverages.append(coverage)
    labels = np.stack(errors).argmin(axis=0).astype(np.int16)
    best_error = np.take_along_axis(np.stack(errors), labels[None], axis=0)[0]
    field = np.take_along_axis(np.stack(coverages), labels[None], axis=0)[0]
    field[~mask] = 0
    meaningful = mask & (field >= .1)
    error_ratio = float((best_error[meaningful] > 16).mean()) if meaningful.any() else 1.
    stroke = field > .5
    count, parts, stats, _ = cv2.connectedComponentsWithStats(np.uint8(stroke), connectivity=4)
    supported = [i for i in range(1, count) if stats[i, cv2.CC_STAT_AREA] >= 4 and field[parts == i].max() >= .8]
    stable = np.isin(parts, supported)
    support_ratio = float(stable.sum() / max(1, stroke.sum()))
    distance = cv2.distanceTransform(np.uint8(stable), cv2.DIST_L2, 5)
    thin_ratio = float((distance[stable] <= 2.5).mean()) if stable.any() else 0.
    solid_ratio = float((field[stable] >= .8).mean()) if stable.any() else 0.
    occupancy = float(stable.mean())
    if error_ratio > .03 or support_ratio < .95 or thin_ratio < .8 or solid_ratio < .6 or occupancy > .2 or not supported or len(supported) > 256:
        raise SkippedInput('not-line-art', 'Stroke mixture, continuity or width validation failed.',
                           dict(stroke_color_error_ratio=error_ratio, stroke_support_ratio=support_ratio,
                                thin_stroke_ratio=thin_ratio, solid_stroke_ratio=solid_ratio, stroke_occupancy=occupancy))
    field[~stable] = 0
    # Preserve bounded white lettering inside a colored stamp, not the large
    # transparent openings enclosed by black line drawings.
    for color in range(1, len(palette)):
        part = np.uint8(stable & (labels == color))
        padded = np.pad(part, 1)
        filled = padded.copy()
        cv2.floodFill(filled, None, (0, 0), 1)
        holes = (filled[1:-1, 1:-1] == 0) & mask & (rgb.min(axis=2) >= 235)
        if 0 < holes.sum() <= part.sum() * .3:
            if palette[-1] != [255, 255, 255]:
                palette.append([255, 255, 255])
            labels[holes] = len(palette) - 1
            field[holes], stable[holes] = 1., True
    labels[~stable] = -1
    ys, xs = np.where(stable)
    cropped = labels[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    height, width = cropped.shape
    scale = min(1., MAX_GEOMETRY_DIMENSION / max(width, height))
    grid = cv2.resize(cropped, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation=cv2.INTER_NEAREST)
    metrics = dict(mode='line-art-palette', color_fidelity='palette', palette=palette,
                   foreground_pixels=int(stable.sum()), background=background, alpha_threshold=threshold,
                   source_geometry_resolution=[width, height], mesh_resolution=[grid.shape[1], grid.shape[0]],
                   stroke_color_error_ratio=error_ratio, stroke_support_ratio=support_ratio,
                   thin_stroke_ratio=thin_ratio, stroke_occupancy=occupancy,
                   solid_stroke_ratio=solid_ratio,
                   stroke_width_median_px=float(2 * np.median(distance[stable])),
                   line_art_white_matte_removed_pixels=int((mask & ~stable).sum()))
    return dict(palette=palette, labels=grid.tolist(), _source_labels=cropped.copy(),
                _stroke_coverage=field, edge_coverage=cv2.resize(field[ys.min():ys.max() + 1, xs.min():xs.max() + 1],
                (grid.shape[1], grid.shape[0]), interpolation=cv2.INTER_AREA).tolist(), analysis=metrics)


def recover_cartoon_palette(path):
    """Recover discrete fills only after legacy palette/line-art rejection."""
    import cv2
    import numpy as np
    from PIL import Image

    # Keep the existing all-foreground texture/noise rejection as a prerequisite.
    result = analyze_dominant_color(path)
    with Image.open(path) as image:
        rgba = np.array(image.convert('RGBA'))
    rgb, alpha, mask, background, _ = _foreground(rgba)
    count, parts, stats, _ = cv2.connectedComponentsWithStats(np.uint8(mask), connectivity=4)
    mask &= np.isin(parts, [i for i in range(1, count) if stats[i, cv2.CC_STAT_AREA] >= 4])
    interior = cv2.erode(np.uint8(mask), np.ones((3, 3), np.uint8)).astype(bool)
    for axis in (0, 1):
        delta = np.diff(rgb.astype(np.int16), axis=axis)
        pairs = (interior[:-1] & interior[1:]) if axis == 0 else (interior[:, :-1] & interior[:, 1:])
        magnitude = np.max(abs(delta), axis=2)
        varying = pairs & (magnitude > 0) & (magnitude <= 12)
        if varying.sum() >= max(4, pairs.sum() * .05):
            changes = delta[varying].astype(float)
            coherence = np.max(abs(changes.sum(0)) / np.maximum(1, abs(changes).sum(0)))
            if coherence >= .9:
                raise SkippedInput('cartoon-directional-gradient', 'Directional gradients are not discrete cartoon fills.')

    variation = cv2.dilate(rgb, np.ones((3, 3), np.uint8)).astype(np.int16) - cv2.erode(rgb, np.ones((3, 3), np.uint8)).astype(np.int16)
    samples = interior & (variation.max(2) <= 24)
    if samples.sum() < mask.sum() * .2:
        raise SkippedInput('cartoon-no-flat-seeds', 'Insufficient locally flat fill support.')
    colors, frequencies = np.unique(rgb[samples], axis=0, return_counts=True)
    palette = []
    for index in np.argsort(-frequencies, kind='stable'):
        if frequencies[index] < max(4, samples.sum() * .0005):
            break
        color = colors[index].astype(int)
        if not palette or np.max(abs(np.array(palette) - color), axis=1).min() > 24:
            palette.append(color.tolist())
        if len(palette) > MAX_STABLE_COLORS:
            raise SkippedInput('cartoon-too-many-colors', 'Too many independent flat fill colors.')
    # Thin dark ink may have no 3x3 interior: it needs connected source support.
    dark = mask & (rgb.max(2) <= 64)
    ink_count, ink_parts, ink_stats, _ = cv2.connectedComponentsWithStats(np.uint8(dark), connectivity=4)
    dark &= np.isin(ink_parts, [i for i in range(1, ink_count) if ink_stats[i, cv2.CC_STAT_AREA] >= 4])
    if dark.any():
        neutral_ink = dark & (np.ptp(rgb, axis=2) <= 24)
        ink = np.median(rgb[neutral_ink if neutral_ink.any() else dark], axis=0).astype(int)
        if not palette or np.max(abs(np.array(palette) - ink), axis=1).min() > 24:
            palette.append(ink.tolist())
    if not 2 <= len(palette) <= MAX_STABLE_COLORS:
        raise SkippedInput('cartoon-palette-size', 'Recovery requires a bounded multicolor palette.')

    distances = np.stack([np.sum((rgb.astype(np.int32) - color) ** 2, axis=2) for color in np.array(palette)])
    labels = distances.argmin(0).astype(np.int16)
    darkest = int(np.argmin(np.mean(palette, axis=1)))
    labels[dark] = darkest
    labels[~mask] = -1
    original_error = np.max(abs(np.array(palette)[np.maximum(labels, 0)] - rgb.astype(np.int16)), axis=2)
    protected = preserve_stroke_labels(rgb, labels, palette, mask, mask & (original_error > 12))
    reassigned = 0
    # Absorb isolated transition specks into touching fills; retain supported ink
    # and contrasting accents. Never change the silhouette or erase entire colors.
    for color in range(len(palette)):
        number, regions, areas, _ = cv2.connectedComponentsWithStats(np.uint8(labels == color), connectivity=4)
        for region in range(1, number):
            area = areas[region, cv2.CC_STAT_AREA]
            if area >= MIN_STABLE_REGION_AREA:
                continue
            patch = regions == region
            border = cv2.dilate(np.uint8(patch), np.ones((3, 3), np.uint8)).astype(bool) & mask & ~patch
            neighbors, votes = np.unique(labels[border], return_counts=True)
            valid = (neighbors >= 0) & (neighbors != color)
            neighbors, votes = neighbors[valid], votes[valid]
            if not len(neighbors):
                continue
            contrast = np.max(abs(np.array(palette)[neighbors] - palette[color]), axis=1).min()
            if protected[patch].any() or (area >= 4 and contrast >= 64 and int((samples & patch).sum()) >= 4):
                continue
            observed = np.mean(rgb[patch], axis=0)
            errors = np.max(abs(np.array(palette)[neighbors] - observed), axis=1)
            best = np.lexsort((-votes, errors))[0]
            labels[patch] = neighbors[best]
            reassigned += int(area)
    labels[dark] = darkest
    reconstruction = np.array(palette, dtype=np.int16)[np.maximum(labels, 0)]
    error = np.max(abs(reconstruction - rgb.astype(np.int16)), axis=2)
    # Boundary pixels may be a physical antialias blend of the two touching
    # fills. Test that mixture locally instead of inventing extra materials.
    observed = rgb.astype(np.float32)
    for color in range(len(palette)):
        near = cv2.dilate(np.uint8(labels == color), np.ones((5, 5), np.uint8)).astype(bool) & mask
        first = np.array(palette[color], dtype=np.float32)
        vector = reconstruction.astype(np.float32) - first
        weight = np.clip(((observed - first) * vector).sum(2) / np.maximum(1, (vector * vector).sum(2)), 0, 1)
        mixed = first + weight[:, :, None] * vector
        error[near] = np.minimum(error[near], abs(mixed - observed).max(2)[near])
    flat_ratio = float((error[samples] <= 16).mean())
    error_ratio = float((error[mask] > 48).mean())
    ink_error = float((reconstruction[dark].max(1) > 96).mean()) if dark.any() else 0.
    regions = sum(cv2.connectedComponents(np.uint8(labels == color), connectivity=4)[0] - 1 for color in range(len(palette)))
    metrics = dict(cartoon_flat_color_ratio=flat_ratio, cartoon_color_error_ratio=error_ratio,
                   cartoon_ink_error_ratio=ink_error, cartoon_reassigned_pixels=reassigned,
                   cartoon_regions=regions, cartoon_palette=palette)
    if flat_ratio < .95 or error_ratio > NOISE_BUDGET or ink_error > .03 or regions > 256:
        raise SkippedInput('cartoon-color-validation', 'Cartoon recovery failed fill, ink, noise or region validation.', metrics)
    ys, xs = np.where(mask)
    crop = labels[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    height, width = crop.shape
    scale = min(1., MAX_GEOMETRY_DIMENSION / max(width, height))
    grid = cv2.resize(crop, (max(1, round(width * scale)), max(1, round(height * scale))), interpolation=cv2.INTER_NEAREST)
    filled = repair_diagonal_contacts(grid)
    result.update(palette=palette, labels=grid.tolist(), _source_labels=crop.copy())
    result['analysis'].update(metrics, mode='cartoon-recovered-palette', color_fidelity='palette',
                              palette=palette, color_regions=regions, filled_diagonal_pixels=filled)
    result['analysis'].pop('dominant_color', None)
    return result


def analyze_dominant_color(path):
    """Validate smooth color structure, then extrude with one dominant flat fill."""
    import cv2
    import numpy as np
    from PIL import Image

    with Image.open(path) as image:
        if image.format != 'PNG':
            raise SkippedInput('unsupported-format', 'Only PNG input is supported.')
        if image.width * image.height > 4_000_000:
            raise SkippedInput('image-too-large', 'Input exceeds 4 million pixels; resize before retrying.')
        rgba = np.array(image.convert('RGBA'))
    rgb, alpha, mask, background, alpha_threshold = _foreground(rgba)
    count = int(mask.sum())
    metrics = {'foreground_pixels': count, 'background': background,
               'alpha_threshold': alpha_threshold,
               'ignored_semitransparent_pixels': int(((alpha > 0) & (alpha < alpha_threshold)).sum())}
    if count == 0:
        raise SkippedInput('empty-image', 'No visible foreground remains.', metrics)
    if count > VERTEX_MAX_FOREGROUND:
        raise SkippedInput('excessive-geometry', 'Foreground exceeds 200000 pixels; resize before retrying.', metrics)
    number, parts, stats, _ = cv2.connectedComponentsWithStats(mask.astype('uint8'), connectivity=4)
    dust = [i for i in range(1, number) if stats[i, cv2.CC_STAT_AREA] < 4]
    metrics['discarded_silhouette_pixels'] = int(np.isin(parts, dust).sum())
    mask[np.isin(parts, dust)] = False
    if not mask.any():
        raise SkippedInput('too-small', 'No silhouette larger than 3 pixels.', metrics)
    core = cv2.erode(mask.astype('uint8'), np.ones((5, 5), np.uint8)).astype(bool)
    interior = core.copy()
    if not core.any():
        core = mask.copy()
    # Snap edge pixels to core colors for the smoothness check, avoiding matte fringes.
    _, nearest = cv2.distanceTransformWithLabels(np.uint8(~core), cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
    snapped = rgb.copy()
    outside = ~core
    snapped[outside] = rgb[core][nearest[outside] - 1]
    ys, xs = np.where(mask)
    top, left = ys.min(), xs.min()
    source_mask = mask[top:ys.max() + 1, left:xs.max() + 1]
    cropped_rgb = snapped[top:ys.max() + 1, left:xs.max() + 1]
    source_height, source_width = source_mask.shape
    scale = min(1, MAX_GEOMETRY_DIMENSION / max(source_width, source_height))
    if scale < 1:
        size = (max(1, round(source_width * scale)), max(1, round(source_height * scale)))
        cropped_rgb = cv2.resize(cropped_rgb, size, interpolation=cv2.INTER_AREA)
        cell_mask = cv2.resize(source_mask.astype('uint8'), size, interpolation=cv2.INTER_NEAREST).astype(bool)
    else:
        cell_mask = source_mask
    cells_height, cells_width = cell_mask.shape
    vertex_grid = cv2.resize(cropped_rgb, (cells_width + 1, cells_height + 1), interpolation=cv2.INTER_AREA)
    # Analysis-only reconstruction rejects noise; these samples are never exported.
    reconstruction = cv2.resize(vertex_grid, (source_width, source_height), interpolation=cv2.INTER_LINEAR)
    error = np.max(np.abs(rgb[top:ys.max() + 1, left:xs.max() + 1].astype(np.int16)
                          - reconstruction.astype(np.int16)), axis=2)
    errors = error[source_mask]
    over_limit = float((errors > VERTEX_RECONSTRUCTION_ERROR_LIMIT).mean())
    metrics['vertex_reconstruction_p50'] = int(np.percentile(errors, 50))
    metrics['vertex_reconstruction_p95'] = int(np.percentile(errors, 95))
    metrics['vertex_reconstruction_over_limit'] = over_limit
    # Keep edge-heavy line artwork distinguishable from interior texture. The
    # global rejection threshold still applies: a small clean core cannot hide
    # noisy strokes or erase contrasting fine details through a dominant fill.
    crop = np.s_[top:ys.max() + 1, left:xs.max() + 1]
    opened = cv2.morphologyEx(mask.astype('uint8'), cv2.MORPH_OPEN, np.ones((5, 5), np.uint8)).astype(bool)
    regions = {'interior': interior, 'edge': mask & opened & ~interior,
               'thin_detail': mask & ~opened}
    for name, region in regions.items():
        values = error[region[crop]]
        metrics[f'{name}_pixels'] = int(region.sum())
        metrics[f'{name}_reconstruction_over_limit'] = float((values > VERTEX_RECONSTRUCTION_ERROR_LIMIT).mean()) if len(values) else None
    metrics['color_rejection_scope'] = 'all-foreground'
    if over_limit > VERTEX_RECONSTRUCTION_OVER_LIMIT:
        raise SkippedInput('gradient-or-texture',
                           f'Color reconstruction deviates on {over_limit:.0%} of pixels; noise or complex texture.',
                           metrics)
    labels = np.where(cell_mask, 0, -1).astype(np.int16)
    filled_diagonal_pixels = repair_diagonal_contacts(labels)
    islands = cv2.connectedComponents(mask.astype('uint8'), connectivity=4)[0] - 1
    metrics['filled_diagonal_pixels'] = filled_diagonal_pixels
    metrics['source_geometry_resolution'] = [source_width, source_height]
    metrics['mesh_resolution'] = [cells_width, cells_height]
    palette = [dominant_rgb(rgb, mask)]
    metrics['mode'] = 'dominant-color'
    metrics['dominant_color'] = palette[0]
    coverage = edge_coverage(alpha, mask, background, (left, top, xs.max() + 1, ys.max() + 1),
                             (cells_width, cells_height))
    return {'labels': labels.tolist(), '_source_labels': np.where(source_mask, 0, -1).astype(np.int16),
            'palette': palette, 'edge_coverage': coverage,
            'silhouette_islands': islands, 'analysis': metrics}


def classify_image(path, color=None, refine_colors=False, merge_colors=False):
    import cv2
    import numpy as np
    from PIL import Image

    with Image.open(path) as image:
        if image.format != 'PNG':
            raise SkippedInput('unsupported-format', 'Only PNG input is supported.')
        if image.width * image.height > 4_000_000:
            raise SkippedInput('image-too-large', 'Input exceeds 4 million pixels; resize before retrying.')
        rgba = np.array(image.convert('RGBA'))
    rgb, alpha, mask, background, alpha_threshold = _foreground(rgba)
    count = int(mask.sum())
    metrics = {'foreground_pixels': count, 'background': background,
               'alpha_threshold': alpha_threshold,
               'ignored_semitransparent_pixels': int(((alpha > 0) & (alpha < alpha_threshold)).sum())}
    if count == 0:
        raise SkippedInput('empty-image', 'No visible foreground remains.', metrics)
    if count > 200_000:
        raise SkippedInput('excessive-geometry', 'Foreground exceeds 200000 pixels; resize before retrying.', metrics)
    number, parts, stats, _ = cv2.connectedComponentsWithStats(mask.astype('uint8'), connectivity=4)
    dust = [i for i in range(1, number) if stats[i, cv2.CC_STAT_AREA] < 4]
    metrics['discarded_silhouette_pixels'] = int(np.isin(parts, dust).sum())
    mask[np.isin(parts, dust)] = False
    if not mask.any():
        raise SkippedInput('too-small', 'No silhouette larger than 3 pixels.', metrics)
    if color:
        palette = [[int(color[i:i + 2], 16) for i in (1, 3, 5)]]
        labels = np.zeros(mask.shape, dtype=np.int16)
        metrics['mode'] = 'color-override'
        protected_strokes = np.zeros(mask.shape, dtype=bool)
    else:
        # Low-contrast gradients can otherwise pass the flat-palette tolerance.
        interior = cv2.erode(mask.astype('uint8'), np.ones((3, 3), np.uint8)).astype(bool)
        for axis in (0, 1):
            delta = np.diff(rgb.astype(np.int16), axis=axis)
            pairs = (interior[:-1] & interior[1:]) if axis == 0 else (interior[:, :-1] & interior[:, 1:])
            magnitude = np.max(abs(delta), axis=2)
            varying = pairs & (magnitude > 0) & (magnitude <= 12)
            if int(varying.sum()) < max(4, int(pairs.sum()) * 0.05):
                continue
            changes = delta[varying].astype(np.float64)
            coherence = np.max(abs(changes.sum(axis=0)) / np.maximum(1, abs(changes).sum(axis=0)))
            if coherence >= 0.9:
                raise SkippedInput('gradient-or-texture', 'Detected a smooth directional color gradient.', metrics)
        # Frequent actual colors preserve small, stable regions without inventing
        # a color palette from an unrestricted photo/gradient quantization.
        samples = mask
        if merge_colors:
            # Sample locally continuous fills, not the intermediate colors along
            # outlines. This also keeps a 4x4 contrasting detail (2x2 interior).
            kernel = np.ones((3, 3), np.uint8)
            variation = cv2.dilate(rgb, kernel).astype(np.int16) - cv2.erode(rgb, kernel).astype(np.int16)
            samples = cv2.erode(mask.astype('uint8'), kernel).astype(bool) & (variation.max(axis=2) <= 24)
            metrics.update(palette_sample_method='local-flat-3x3', palette_sample_pixels=int(samples.sum()))
        pixels = rgb[samples]
        if len(pixels) == 0:
            raise SkippedInput('translucent-colors', 'No opaque colors available.', metrics)
        colors, frequencies = np.unique(pixels, axis=0, return_counts=True)
        palette = []
        for index in np.argsort(-frequencies):
            support = int(frequencies[index])
            if support < (4 if merge_colors else max(16, len(pixels) * 0.0018)):
                break
            candidate = colors[index].astype(float)
            if candidate.min() >= 245:
                matches = np.max(abs(rgb.astype(float) - candidate), axis=2) <= 6
                inner = cv2.erode(mask.astype('uint8'), np.ones((5, 5), np.uint8)).astype(bool)
                if int((matches & inner).sum()) < 8:
                    continue  # White matte fringe, not a white interior color.
            # Preserve close but large flat fills; merge small anti-alias variants.
            tolerance = 32 if merge_colors else ((6 if refine_colors else 3) if support >= len(pixels) * 0.015 else 64)
            distance = float(np.min(np.max(abs(np.array(palette) - candidate), axis=1))) if palette else 256
            if merge_colors and 32 < distance <= 64:
                support_mask = samples & (np.max(abs(rgb.astype(np.float64) - candidate), axis=2) <= 16)
                number, _, stats, _ = cv2.connectedComponentsWithStats(support_mask.astype('uint8'), connectivity=4)
                largest = int(stats[1:, cv2.CC_STAT_AREA].max()) if number > 1 else 0
                if largest < 64:
                    metrics['ignored_transition_candidates'] = metrics.get('ignored_transition_candidates', 0) + 1
                    continue
            if distance > tolerance:
                palette.append(candidate.astype(int).tolist())
            if len(palette) > MAX_STABLE_COLORS:
                raise SkippedInput('too-many-colors', f'More than {MAX_STABLE_COLORS} stable colors; possible gradient or complex artwork.',
                                   {**metrics, 'detected_colors_at_least': len(palette), 'detected_palette': palette})
        if not palette:
            raise SkippedInput('complex-colors', 'No sufficiently stable flat colors; possible texture or gradient.', metrics)
        delta = [rgb.astype(np.int32) - c for c in np.array(palette)]
        labels = np.stack([np.sum(d * d, axis=2) for d in delta]).argmin(0).astype(np.int16)
        error = np.stack([np.max(abs(d), axis=2) for d in delta]).min(0)
        kernel = np.ones((5, 5), np.uint8)
        interior = cv2.erode(mask.astype('uint8'), kernel).astype(bool)
        interior &= cv2.dilate(labels.astype('uint8'), kernel) == cv2.erode(labels.astype('uint8'), kernel)
        flat_limit = 16 if merge_colors else 6
        flat_ratio = float((error[interior] <= flat_limit).mean()) if interior.any() else 0
        core_ratio = float(interior.sum() / count)
        interior_kernel = 5
        if refine_colors and core_ratio < 0.2:
            # A three-pixel stroke has no 5x5 interior. Require the same flat
            # color ratio and interior coverage when checking a 3x3 interior.
            kernel = np.ones((3, 3), np.uint8)
            interior = cv2.erode(mask.astype('uint8'), kernel).astype(bool)
            interior &= cv2.dilate(labels.astype('uint8'), kernel) == cv2.erode(labels.astype('uint8'), kernel)
            flat_ratio = float((error[interior] <= flat_limit).mean()) if interior.any() else 0
            core_ratio = float(interior.sum() / count)
            interior_kernel = 3
        metrics.update(color_refinement=refine_colors, near_color_tolerance=32 if merge_colors else 6 if refine_colors else 3,
                       interior_kernel=interior_kernel, flat_color_error_limit=flat_limit)
        metrics.update(mode='automatic-palette', palette=palette, flat_color_ratio=flat_ratio, stable_interior_ratio=core_ratio)
        if core_ratio < 0.2 or flat_ratio < 0.95:
            raise SkippedInput('gradient-or-texture', 'Insufficient flat-color interiors; gradient, texture, or very fine artwork.', metrics)
        confident = mask & (error <= 12)
        fringe = mask & ~confident
        metrics['antialias_pixels'] = int(fringe.sum())
        if fringe.sum() > count * FRINGE_BUDGET or not confident.any():
            raise SkippedInput('gradient-or-texture', 'Too much color interpolation outside the flat palette.', metrics)
        # Retain the closest palette assignment; copying distant flat interiors
        # over antialias pixels can erase a one-pixel outline entirely.
        # Textured fills can resemble partial dark ink; extend strokes only
        # when the already validated flat interiors are especially reliable.
        protected_strokes = (preserve_stroke_labels(rgb, labels, palette, mask, fringe)
                             if flat_ratio >= .995 else np.zeros(mask.shape, dtype=bool))
        metrics['protected_stroke_pixels'] = int(protected_strokes.sum())

    labels[~mask] = -1
    regions = 0
    stable = np.zeros(mask.shape, dtype=bool)
    for index in range(len(palette)):
        number, parts, stats, _ = cv2.connectedComponentsWithStats(np.uint8(labels == index), connectivity=4)
        keep = [i for i in range(1, number) if stats[i, cv2.CC_STAT_AREA] >= MIN_STABLE_REGION_AREA]
        stable |= np.isin(parts, keep)
        regions += number - 1
    stable |= protected_strokes
    noise = mask & ~stable
    metrics['reassigned_color_pixels'] = int(noise.sum())
    if noise.sum() > count * NOISE_BUDGET or not stable.any():
        raise SkippedInput('fragmented-colors', 'Too many color details smaller than 16 pixels.', metrics)
    if noise.any():
        _, nearest = cv2.distanceTransformWithLabels(np.uint8(~stable), cv2.DIST_L2, 5, labelType=cv2.DIST_LABEL_PIXEL)
        labels[noise] = labels[stable][nearest[noise] - 1]
    regions = sum(cv2.connectedComponents(np.uint8(labels == i), connectivity=4)[0] - 1 for i in range(len(palette)))
    metrics['color_regions'] = regions
    if regions > 256:
        raise SkippedInput('fragmented-colors', 'More than 256 disconnected color regions.', metrics)
    islands, _ = cv2.connectedComponents(mask.astype('uint8'), connectivity=4)
    ys, xs = np.where(mask)
    cropped = labels[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    source_height, source_width = cropped.shape
    source_labels = cropped.copy()
    scale = min(1, MAX_GEOMETRY_DIMENSION / max(source_width, source_height))
    if scale < 1:
        size = (max(1, round(source_width * scale)), max(1, round(source_height * scale)))
        cropped = cv2.resize((cropped + 1).astype('uint8'), size, interpolation=cv2.INTER_NEAREST).astype(np.int16) - 1
    filled_diagonal_pixels = repair_diagonal_contacts(cropped)
    metrics['filled_diagonal_pixels'] = filled_diagonal_pixels
    metrics['source_geometry_resolution'] = [source_width, source_height]
    metrics['mesh_resolution'] = [cropped.shape[1], cropped.shape[0]]
    coverage = edge_coverage(alpha, mask, background, (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1),
                             (cropped.shape[1], cropped.shape[0]))
    return {'palette': palette, 'labels': cropped.tolist(), '_source_labels': source_labels, 'edge_coverage': coverage,
            'silhouette_islands': islands - 1, 'analysis': metrics}
