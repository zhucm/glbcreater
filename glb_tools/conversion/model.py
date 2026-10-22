"""Extracted flat-color geometry and Blender renderer; no resource registration."""
from collections import Counter
import json
import importlib.util
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import struct
import sys

color_spec = importlib.util.spec_from_file_location('model_colors', Path(__file__).with_name('model-colors.py'))
color_tools = importlib.util.module_from_spec(color_spec)
color_spec.loader.exec_module(color_tools)

class GenerationSkipped(Exception):
    """Raised when Blender reports geometry that must be skipped, not failed."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason
        self.message = message


class BlenderFailure(ValueError):
    """Raised when the Blender subprocess exits nonzero."""

    def __init__(self, exit_code, log_path, note=''):
        super().__init__(f'Blender failed ({exit_code}); see {log_path}.' + note)
        self.detail = f'Blender exit code {exit_code}; see blender.log.'


def run_model_with_recovery(run, analysis, *, image_path, color, **options):
    if analysis['analysis'].get('mode') != 'cartoon-recovered-palette':
        return analysis, run_blender_job(run, analysis, **options)
    attempt = run / 'cartoon-attempt'
    attempt.mkdir(exist_ok=False)
    try:
        report = run_blender_job(attempt, analysis, **options)
    except (GenerationSkipped, ValueError) as error:
        analysis = color_tools.analyze_image(image_path, color)
        analysis['analysis'].update(cartoon_recovery_rejected='cartoon-mesh-validation',
                                   cartoon_recovery_message=str(error))
        return analysis, run_blender_job(run, analysis, **options)
    for name in (options['name'] + '.glb', options['name'] + '.blend', 'preview-96.png', 'validation.json', 'job.json'):
        shutil.copyfile(attempt / name, run / name)
    return analysis, report


def run_blender_job(run, analysis, *, name, title, width_cm, thickness_cm, blender, fail_note=''):
    """Model one analyzed image inside the given run directory via Blender."""
    job = dict(name=name, title=title, width_cm=width_cm,
               thickness_cm=thickness_cm, **analysis)
    (run / 'job.json').write_text(json.dumps(job, ensure_ascii=False), encoding='utf-8')
    command = [str(Path(blender).resolve()), '--background', '--factory-startup',
               '--python-exit-code', '1', '--python', str(Path(__file__).resolve()),
               '--', '--blender-job', str(run / 'job.json')]
    print(f'Generating model locally. Authoring files and log: {run}', flush=True)
    with (run / 'blender.log').open('w', encoding='utf-8') as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                timeout=300,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        geometry_skip = run / 'geometry-skip.json'
        if geometry_skip.is_file():
            detail = json.loads(geometry_skip.read_text(encoding='utf-8'))
            raise GenerationSkipped(detail['reason'], detail['message'])
        raise BlenderFailure(result.returncode, run / 'blender.log', fail_note)
    report = json.loads((run / 'validation.json').read_text(encoding='utf-8'))
    outputs = [f'{name}.glb', f'{name}.blend', 'preview-96.png']
    for filename in outputs:
        if not (run / filename).is_file() or (run / filename).stat().st_size == 0:
            raise ValueError(f'Missing Blender output: {filename}; no publication performed.')
    from PIL import Image
    with Image.open(run / 'preview-96.png') as preview:
        alpha = preview.getchannel('A') if preview.mode == 'RGBA' else None
        if preview.size != (96, 96) or alpha is None or alpha.getextrema()[0] == 255:
            raise ValueError('preview-96.png must be a 96x96 RGBA image with transparency.')
    return report


def partition_mesh(labels, coverage=None):
    """Shared front/back faces and outer walls; no walls at material boundaries."""
    height, width = len(labels), len(labels[0])
    components = [[0] * width for _ in range(height)]
    component = 0
    for y in range(height):
        for x in range(width):
            if labels[y][x] < 0 or components[y][x]:
                continue
            component += 1
            components[y][x] = component
            pending = [(x, y)]
            while pending:
                px, py = pending.pop()
                for nx, ny in ((px - 1, py), (px + 1, py), (px, py - 1), (px, py + 1)):
                    if (0 <= nx < width and 0 <= ny < height and labels[ny][nx] >= 0
                            and not components[ny][nx]):
                        components[ny][nx] = component
                        pending.append((nx, ny))
    faces, materials, points, point_ids, boundaries, outline = [], [], [], {}, {}, {}

    def point_id(x, y, component):
        key = (component, x, y)
        if key not in point_ids:
            point_ids[key] = len(points)
            points.append((float(x), float(y)))
        return point_ids[key]

    for y, row in enumerate(labels):
        for x, material in enumerate(row):
            if material < 0:
                continue
            corners = [(x, y), (x, y + 1), (x + 1, y + 1), (x + 1, y)]
            component = components[y][x]
            ids = [point_id(*p, component) for p in corners]
            faces.extend([tuple(2 * i for i in ids), tuple(2 * i + 1 for i in reversed(ids))])
            materials.extend([material, material])
            for edge, (nx, ny) in enumerate(((x - 1, y), (x, y + 1), (x + 1, y), (x, y - 1))):
                neighbor = labels[ny][nx] if 0 <= nx < width and 0 <= ny < height else -1
                a, b = ids[edge], ids[(edge + 1) % 4]
                if neighbor != material:
                    boundaries.setdefault(a, set()).add(b)
                    boundaries.setdefault(b, set()).add(a)
                if neighbor < 0:
                    outline.setdefault(a, set()).add(b)
                    outline.setdefault(b, set()).add(a)
                    faces.append((2 * a, 2 * a + 1, 2 * b + 1, 2 * b))
                    materials.append(material)
    original = list(points)
    protected = set()
    for index, neighbors in outline.items():
        if len(neighbors) != 2:
            protected.add(index)
            continue
        x, y = original[index]
        occupied = lambda cx, cy: 0 <= cx < width and 0 <= cy < height and labels[cy][cx] >= 0
        # Freeze ends and sides of one-cell strokes, including narrow hole walls.
        if any(occupied(cx, cy) and
               ((not occupied(cx - 1, cy) and not occupied(cx + 1, cy)) or
                (not occupied(cx, cy - 1) and not occupied(cx, cy + 1)))
               for cx, cy in ((int(x) - 1, int(y) - 1), (int(x), int(y) - 1),
                              (int(x) - 1, int(y)), (int(x), int(y)))):
            protected.add(index)
            continue
        a, b = neighbors
        vectors = [(original[n][0] - x, original[n][1] - y) for n in (a, b)]
        # A sustained right-angle turn is a real corner; alternating turns are stairs.
        if sum(vectors[0][axis] * vectors[1][axis] for axis in (0, 1)) == 0:
            sustained = True
            for n, vector in zip((a, b), vectors):
                following = outline[n] - {index}
                if len(following) != 1:
                    sustained = False
                    break
                p = original[next(iter(following))]
                sustained &= (p[0] - original[n][0], p[1] - original[n][1]) == vector
            if sustained:
                protected.add(index)
    # Shared vertices prevent cracks; outer edges ignore material junctions.
    for _ in range(2):
        updated = list(points)
        for index, neighbors in boundaries.items():
            neighbors = outline.get(index, neighbors)
            if len(neighbors) == 2 and index not in protected:
                a, b = neighbors
                updated[index] = tuple(max(original[index][axis] - 0.35,
                                           min(original[index][axis] + 0.35,
                                               points[index][axis] * 0.5 +
                                               (points[a][axis] + points[b][axis]) * 0.25)) for axis in (0, 1))
        points = updated
    if coverage is not None:
        def sample(x, y):
            import math
            x, y = x - 0.5, y - 0.5
            ix, iy = math.floor(x), math.floor(y)
            fx, fy = x - ix, y - iy
            return sum(coverage[cy][cx] * wx * wy
                       for cx, wx in ((ix, 1 - fx), (ix + 1, fx))
                       for cy, wy in ((iy, 1 - fy), (iy + 1, fy))
                       if 0 <= cx < width and 0 <= cy < height)

        smoothed = list(points)
        for index, neighbors in outline.items():
            if index in protected or len(neighbors) != 2:
                continue
            a, b = neighbors
            dx, dy = smoothed[b][0] - smoothed[a][0], smoothed[b][1] - smoothed[a][1]
            length = (dx * dx + dy * dy) ** 0.5
            if length == 0:
                continue
            nx, ny = -dy / length, dx / length
            x, y = smoothed[index]
            candidates = [(x + nx * step / 100, y + ny * step / 100) for step in range(-35, 36, 5)]
            candidates = [p for p in candidates if all(abs(p[k] - original[index][k]) <= 0.35 for k in (0, 1))]
            best = min(candidates, key=lambda p: (abs(sample(*p) - 0.5), (p[0] - x) ** 2 + (p[1] - y) ** 2))
            # Do not extrapolate a missing contour or pull a hole into translucent artwork.
            values = [sample(*p) for p in candidates]
            if min(values) <= 0.5 <= max(values):
                points[index] = best
    return points, faces, materials


def contour_mesh(job):
    """Triangulate each independent solid without welding touching islands."""
    loops = job['contours']

    def contains(loop, point):
        x, y = point
        return sum((a[1] > y) != (b[1] > y) and
                   x < a[0] + (y - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
                   for a, b in zip(loop, loop[1:] + loop[:1])) % 2 == 1

    parents = [[j for j, other in enumerate(loops) if i != j and contains(other, loop[0])]
               for i, loop in enumerate(loops)]
    points, faces, materials, diagnostics = [], [], [], []
    for index, loop in enumerate(loops):
        if len(parents[index]) % 2:
            continue
        hole_ids = [i for i in range(len(loops))
                    if index in parents[i] and len(parents[i]) == len(parents[index]) + 1]
        component_loops = [loop, *(loops[i] for i in hole_ids)]
        try:
            component_points, component_faces, component_materials, diagnostic = contour_component(job, component_loops)
        except ValueError as error:
            if str(error) != 'Shared boundary material regions disagree after triangulation.' or not job.get('raw_contours'):
                raise
            record = {'component': index, 'reason': str(error), 'mode': 'raw-shared-network', 'status': 'attempted'}
            job['analysis'].setdefault('component_boundary_fallbacks', []).append(record)
            # Keep the component's silhouette, holes and color interfaces in one
            # coordinate network. Do not remove constraints or change its palette.
            retry = dict(job, color_boundaries=[dict(path, points=path['raw_points']) for path in job['color_boundaries']])
            raw_loops = [job['raw_contours'][i] for i in [index, *hole_ids]]
            try:
                component_points, component_faces, component_materials, diagnostic = contour_component(retry, raw_loops)
            except ValueError:
                record['status'] = 'rejected'
                raise
            record['status'] = 'accepted'
        offset = len(points) * 2
        points.extend(component_points)
        faces.extend(tuple(i + offset for i in face) for face in component_faces)
        materials.extend(component_materials)
        diagnostics.append(diagnostic)
    job['analysis']['triangulation'] = diagnostics
    return points, faces, materials


def contour_component(job, loops):
    """Constrained caps and shared boundary walls, including holes and islands."""
    from mathutils import Vector
    from mathutils.geometry import delaunay_2d_cdt

    labels = job['surface_labels']
    height, width = len(labels), len(labels[0])
    points, edges, ids, edge_sides = [], set(), {}, {}

    def point_id(point):
        key = tuple(point)
        if key not in ids:
            ids[key] = len(points)
            points.append(Vector(key))
        return ids[key]

    def edge(a, b, colors=None):
        a, b = point_id(a), point_id(b)
        if a != b:
            key = tuple(sorted((a, b)))
            edges.add(key)
            if colors is not None:
                edge_sides[key] = tuple(colors) if a < b else tuple(reversed(colors))

    x_min = min(p[0] for loop in loops for p in loop)
    x_max = max(p[0] for loop in loops for p in loop)
    y_min = min(p[1] for loop in loops for p in loop)
    y_max = max(p[1] for loop in loops for p in loop)
    for loop in loops:
        for a, b in zip(loop, loop[1:] + loop[:1]):
            edge(a, b)
    contour_edges = set(edges)
    if 'color_boundaries' in job:
        for path in job['color_boundaries']:
            for a, b in zip(path['points'], path['points'][1:]):
                edge(a, b, path['colors'])
    else:
        # Compatibility for historical jobs that only stored a pixel grid.
        for y in range(height):
            for x in range(width):
                if x + 1 < width and labels[y][x] != labels[y][x + 1]:
                    edge((x + 1., y_min if y == 0 else float(y)),
                         (x + 1., y_max if y == height - 1 else y + 1.))
                if y + 1 < height and labels[y][x] != labels[y + 1][x]:
                    edge((x_min if x == 0 else float(x), y + 1.),
                         (x_max if x == width - 1 else x + 1., y + 1.))
    ordered_edges = sorted(edges)
    vertices, split_edges, triangles, _, edge_origins, _ = delaunay_2d_cdt(
        points, ordered_edges, [], 0, 1e-7, True)
    # CDT can move boundary vertices. Classify faces by crossing tagged contour
    # edges, rather than testing rounded centroids against the original loops.
    contour_ids = {i for i, value in enumerate(ordered_edges) if value in contour_edges}
    barriers = {tuple(sorted(value)) for value, origins in zip(split_edges, edge_origins)
                if contour_ids.intersection(origins)}
    color_ids = {i: edge_sides[value] for i, value in enumerate(ordered_edges) if value in edge_sides}
    material_edges = {}
    for value, origins in zip(split_edges, edge_origins):
        a, b = sorted(value)
        for origin in origins:
            if origin in color_ids:
                first, last = ordered_edges[origin]
                colors = color_ids[origin]
                if (vertices[b] - vertices[a]).dot(points[last] - points[first]) < 0:
                    colors = tuple(reversed(colors))
                key = (a, b)
                if key in material_edges and material_edges[key] != colors:
                    raise ValueError('Overlapping color constraints disagree.')
                material_edges[key] = colors
    neighbors = {}
    for index, triangle in enumerate(triangles):
        for a, b in zip(triangle, triangle[1:] + triangle[:1]):
            neighbors.setdefault(tuple(sorted((a, b))), []).append(index)
    adjacency = [[] for _ in range(len(triangles) + 1)]
    outside = len(triangles)
    for value, linked in neighbors.items():
        if len(linked) == 1:
            linked = [linked[0], outside]
        if len(linked) != 2:
            raise ValueError('CDT edge has more than two incident faces.')
        a, b = linked
        crossing = int(value in barriers)
        adjacency[a].append((b, crossing))
        adjacency[b].append((a, crossing))
    parity, pending = {outside: 0}, [outside]
    while pending:
        current = pending.pop()
        for neighbor, crossing in adjacency[current]:
            expected = parity[current] ^ crossing
            if neighbor in parity:
                if parity[neighbor] != expected:
                    raise ValueError('CDT contour constraints do not form closed regions.')
            else:
                parity[neighbor] = expected
                pending.append(neighbor)

    caps, materials, boundary, cap_ids = [], [], {}, {}
    for index, triangle in enumerate(triangles):
        x = sum(vertices[i].x for i in triangle) / len(triangle)
        y = sum(vertices[i].y for i in triangle) / len(triangle)
        if parity.get(index) != 1:
            continue
        cap_ids[index] = len(caps)
        material = labels[min(height - 1, max(0, math.floor(y)))][min(width - 1, max(0, math.floor(x)))]
        caps.append(triangle)
        materials.append(material)
        for a, b in zip(triangle, triangle[1:] + triangle[:1]):
            boundary.setdefault(tuple(sorted((a, b))), []).append((a, b, material))
    if material_edges:
        parents = list(range(len(caps)))

        def root(index):
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        for value, linked in neighbors.items():
            inside_caps = [cap_ids[i] for i in linked if i in cap_ids]
            if len(inside_caps) == 2 and value not in material_edges:
                parents[root(inside_caps[1])] = root(inside_caps[0])
        seeds = {}
        for (a, b), colors in material_edges.items():
            for index in neighbors.get((a, b), []):
                if index not in cap_ids:
                    continue
                other = next(i for i in triangles[index] if i not in (a, b))
                direction, offset = vertices[b] - vertices[a], vertices[other] - vertices[a]
                color = colors[0 if direction.x * offset.y - direction.y * offset.x > 0 else 1]
                component = root(cap_ids[index])
                if component in seeds and seeds[component] != color:
                    job['analysis'].setdefault('material_conflicts', []).append({
                        'component': component, 'triangle': index,
                        'edge': [list(vertices[a]), list(vertices[b])],
                        'existing_material': seeds[component], 'incoming_material': color,
                        'edge_materials': list(colors)})
                    raise ValueError('Shared boundary material regions disagree after triangulation.')
                seeds[component] = color
        # All faces in one curved region receive its boundary's material. Pixel
        # lookups only supply a fallback for a uniform region without interfaces.
        materials = [seeds.get(root(i), material) for i, material in enumerate(materials)]
    boundary_degrees = Counter(i for value, entries in boundary.items() if len(entries) == 1 for i in value)
    invalid = [i for i, degree in boundary_degrees.items() if degree != 2]
    degenerate = [list(cap) for cap in caps
                  if (vertices[cap[1]] - vertices[cap[0]]).cross(vertices[cap[2]] - vertices[cap[0]]) == 0]
    diagnostic = {'input_contour_edges': len(contour_edges), 'split_contour_edges': len(barriers),
                  'cap_triangles': len(caps), 'boundary_vertices': len(boundary_degrees),
                  'invalid_boundary_vertices': [list(vertices[i]) for i in invalid],
                  'degenerate_triangles': degenerate}
    if invalid or degenerate:
        job['analysis']['triangulation'] = [diagnostic]
        raise ValueError('CDT cap has invalid boundary junctions or zero-area triangles.')
    used = sorted({i for cap in caps for i in cap})
    remap = {index: i for i, index in enumerate(used)}
    mesh_points = [(float(vertices[i].x), float(vertices[i].y)) for i in used]
    faces, face_materials = [], []
    for cap, material in zip(caps, materials):
        face = tuple(remap[i] * 2 for i in cap)
        faces.extend((face, tuple(i + 1 for i in reversed(face))))
        face_materials.extend((material, material))
    for entries in boundary.values():
        if len(entries) == 1:
            a, b, material = entries[0]
            a, b = remap[a] * 2, remap[b] * 2
            faces.append((a, a + 1, b + 1, b))
            face_materials.append(material)
    if not faces:
        raise ValueError('Contour triangulation produced no surface.')
    return mesh_points, faces, face_materials, diagnostic


def build_in_blender(job_path):
    import bmesh
    import bpy
    from mathutils import Vector

    root = job_path.parent
    job = json.loads(job_path.read_text(encoding='utf-8'))
    width, thickness = job['width_cm'] / 100, job['thickness_cm'] / 100
    try:
        points, faces, face_materials = (contour_mesh(job) if job.get('contours') else
                                        partition_mesh(job['labels'], job.get('edge_coverage')))
    except ValueError as error:
        (root / 'geometry-diagnostics.json').write_text(json.dumps({
            'stage': 'triangulation', 'coordinate_space': 'color-grid',
            'contours': job.get('contours', []), 'triangulation': job['analysis'].get('triangulation', []),
            'component_boundary_fallbacks': job['analysis'].get('component_boundary_fallbacks', []),
            'material_conflicts': job['analysis'].get('material_conflicts', []),
            'message': str(error),
        }, indent=2), encoding='utf-8')
        (root / 'geometry-skip.json').write_text(json.dumps({
            'reason': 'unsupported-topology', 'message': str(error),
        }), encoding='utf-8')
        raise
    x_min, x_max = min(p[0] for p in points), max(p[0] for p in points)
    y_min, y_max = min(p[1] for p in points), max(p[1] for p in points)
    scale = width / (x_max - x_min)
    height = (y_max - y_min) * scale
    bpy.context.preferences.filepaths.save_version = 0
    bpy.ops.object.select_all(action='SELECT')
    bpy.ops.object.delete(use_global=False)
    scene = bpy.context.scene
    scene.unit_settings.system = 'METRIC'
    scene.unit_settings.length_unit = 'CENTIMETERS'
    data = bpy.data.meshes.new('Shared color regions')
    vertices = [((x - (x_min + x_max) / 2) * scale, (y_max - y) * scale, depth)
                for x, y in points for depth in (-thickness / 2, thickness / 2)]
    data.from_pydata(vertices, [], faces)
    for polygon, material_index in zip(data.polygons, face_materials):
        polygon.material_index = material_index
    model = bpy.data.objects.new(job['title'], data)
    scene.collection.objects.link(model)
    bpy.context.view_layer.objects.active = model
    model.select_set(True)
    mesh = bmesh.new()
    mesh.from_mesh(model.data)
    topology_checks = []

    def validate_topology(stage):
        edges = sum(not edge.is_manifold for edge in mesh.edges)
        vertices = sum(not vertex.is_manifold for vertex in mesh.verts)
        check = {'stage': stage, 'non_manifold_edges': edges, 'non_manifold_vertices': vertices}
        topology_mismatch = False
        if job.get('contours'):
            remaining = set(mesh.verts)
            components = 0
            while remaining:
                components += 1
                pending = [remaining.pop()]
                while pending:
                    vertex = pending.pop()
                    for edge in vertex.link_edges:
                        neighbor = edge.other_vert(vertex)
                        if neighbor in remaining:
                            remaining.remove(neighbor)
                            pending.append(neighbor)
            euler = len(mesh.verts) - len(mesh.edges) + len(mesh.faces)
            expected_components = job['analysis']['contour_components']
            expected_euler = 2 * expected_components - 2 * job['analysis']['contour_holes']
            check.update(components=components, euler_characteristic=euler,
                         expected_components=expected_components, expected_euler_characteristic=expected_euler)
            topology_mismatch = components != expected_components or euler != expected_euler
        topology_checks.append(check)
        if edges or vertices or topology_mismatch:
            diagnostic = {'stage': stage, 'coordinate_space': 'model-meters',
                          'contour_coordinate_space': 'color-grid', 'contours': job.get('contours', []),
                          'bad_edges': [{'vertices': [list(v.co) for v in edge.verts],
                                         'faces': [[list(v.co) for v in face.verts] for face in edge.link_faces]}
                                        for edge in mesh.edges if not edge.is_manifold],
                          'bad_vertices': [list(v.co) for v in mesh.verts if not v.is_manifold]}
            (root / 'geometry-diagnostics.json').write_text(json.dumps(diagnostic, indent=2), encoding='utf-8')
            message = f'Color partition at {stage} has {edges} non-manifold edges and {vertices} invalid vertices.'
            if topology_mismatch:
                message += ' Contour components or holes differ from the source.'
            (root / 'geometry-skip.json').write_text(json.dumps({
                'reason': 'unsupported-topology', 'message': message, 'topology_checks': topology_checks,
            }), encoding='utf-8')
            mesh.free()
            raise ValueError(message)
        return edges, vertices

    validate_topology('before-simplification')
    bmesh.ops.recalc_face_normals(mesh, faces=list(mesh.faces))
    mesh.normal_update()
    bmesh.ops.dissolve_limit(mesh, angle_limit=0.0001, verts=list(mesh.verts), edges=list(mesh.edges),
                            delimit={'MATERIAL'}, use_dissolve_boundaries=False)
    bmesh.ops.recalc_face_normals(mesh, faces=list(mesh.faces))
    non_manifold, invalid_vertices = validate_topology('after-simplification')
    mesh.to_mesh(model.data)
    mesh.free()
    model.data.update()
    for polygon in model.data.polygons:
        polygon.use_smooth = abs(polygon.normal.z) < 0.5

    for rgb in job['palette']:
        linear = [c / 255 / 12.92 if c / 255 <= 0.04045 else ((c / 255 + 0.055) / 1.055) ** 2.4 for c in rgb]
        material = bpy.data.materials.new('#' + ''.join(f'{c:02x}' for c in rgb))
        material.diffuse_color = (*linear, 1)
        material.use_nodes = True
        shader = material.node_tree.nodes.get('Principled BSDF')
        shader.inputs['Base Color'].default_value = material.diffuse_color
        shader.inputs['Roughness'].default_value = 0.65
        shader.inputs['Specular IOR Level'].default_value = 0.15
        model.data.materials.append(material)
    bpy.context.view_layer.update()
    dimensions = list(model.dimensions)
    if (not math.isclose(dimensions[0], width, rel_tol=1e-5)
            or not math.isclose(dimensions[1], height, rel_tol=1e-5)
            or not math.isclose(dimensions[2], thickness, rel_tol=1e-5)):
        raise ValueError(f'Incorrect model dimensions: {dimensions}')
    glb = root / (job['name'] + '.glb')
    bpy.ops.export_scene.gltf(filepath=str(glb), export_format='GLB', use_selection=True, export_yup=True)
    binary = glb.read_bytes()
    json_length = struct.unpack_from('<I', binary, 12)[0]
    gltf = json.loads(binary[20:20 + json_length])
    if gltf.get('textures') or gltf.get('images'):
        raise ValueError('Unexpected texture in exported GLB.')
    attributes = [primitive.get('attributes', {}) for item in gltf.get('meshes', []) for primitive in item['primitives']]
    if any(any(key.startswith(('TEXCOORD_', 'COLOR_')) for key in attribute) for attribute in attributes):
        raise ValueError('Unexpected UV or vertex color in exported GLB.')

    scene.render.engine = 'CYCLES'
    scene.cycles.samples, scene.cycles.use_denoising = 24, True
    scene.render.resolution_x, scene.render.resolution_y = 1000, 1000
    scene.render.resolution_percentage = 100
    scene.world.color = (0.15, 0.15, 0.15)
    scene.display_settings.display_device = 'sRGB'
    scene.view_settings.view_transform = 'Standard'
    scene.view_settings.look = 'None'
    scene.view_settings.exposure = 0
    scene.view_settings.gamma = 1
    scene.render.image_settings.file_format = 'PNG'
    extent = max(width, height, thickness)
    factor = extent / 0.1
    center = Vector((0, height / 2, 0))

    def aim(obj):
        obj.rotation_euler = (center - obj.location).to_track_quat('-Z', 'Y').to_euler()

    for name, location, energy, size in [
        ('Key', (-0.09, -0.12, 0.18), 0.5, 0.12),
        ('Fill', (0.13, -0.04, 0.08), 0.15, 0.10),
        ('Rim', (0.02, 0.08, 0.15), 0.3, 0.08),
    ]:
        data = bpy.data.lights.new(name, 'AREA')
        data.energy, data.size = energy * factor ** 2, size * factor
        light = bpy.data.objects.new(name, data)
        scene.collection.objects.link(light)
        light.location = center + Vector(location) * factor
        aim(light)
    camera = bpy.data.objects.new('Preview camera', bpy.data.cameras.new('Preview camera'))
    scene.collection.objects.link(camera)
    scene.camera = camera
    camera.data.type = 'ORTHO'
    camera.data.clip_start, camera.data.clip_end = extent / 100, extent * 100
    # Bound the projected box so both portrait and landscape silhouettes fit.
    camera.data.ortho_scale = math.sqrt(width ** 2 + height ** 2 + thickness ** 2) * 1.2
    camera.location = center + Vector((0.085, 0, 0.23)) * factor
    aim(camera)
    bpy.ops.wm.save_as_mainfile(filepath=str(root / (job['name'] + '.blend')))
    camera.data.ortho_scale = max(width, height) * 1.18
    camera.location = center + Vector((0, 0, 0.25)) * factor
    aim(camera)
    scene.render.resolution_x = scene.render.resolution_y = 96
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = True
    scene.render.image_settings.color_mode = 'RGBA'
    scene.render.filepath = str(root / 'preview-96.png')
    # The preview is a base-color reference, not a lighting demonstration. Keep
    # the exported GLB and saved authoring scene lit, and restore shaders even
    # when rendering fails.
    preview_shaders = []
    try:
        for material in model.data.materials:
            tree = material.node_tree
            surface = tree.nodes.get('Material Output').inputs['Surface']
            original = surface.links[0].from_socket
            emission = tree.nodes.new('ShaderNodeEmission')
            emission.inputs['Color'].default_value = material.diffuse_color
            emission.inputs['Strength'].default_value = 1
            preview_shaders.append((tree, surface, original, emission))
            tree.links.new(emission.outputs['Emission'], surface)
        bpy.ops.render.render(write_still=True)
    finally:
        for tree, surface, original, emission in preview_shaders:
            tree.links.new(original, surface)
            tree.nodes.remove(emission)

    bpy.ops.object.select_all(action='DESELECT')
    bpy.ops.import_scene.gltf(filepath=str(glb))
    imported = [obj for obj in bpy.context.selected_objects if obj.type == 'MESH']
    bounds = [obj.matrix_world @ Vector(c) for obj in imported for c in obj.bound_box]
    measured = [max(p[i] for p in bounds) - min(p[i] for p in bounds) for i in range(3)]
    if not all(math.isclose(a, b, rel_tol=1e-5) for a, b in zip(dimensions, measured)):
        raise ValueError(f'GLB reimport dimensions differ: {measured}')
    three_dimensions = [measured[0], measured[2], measured[1]]
    expected_three_dimensions = [dimensions[0], dimensions[2], dimensions[1]]
    if not all(math.isclose(a, b, rel_tol=1e-5) for a, b in zip(expected_three_dimensions, three_dimensions)):
        raise ValueError(f'GLB placement dimensions differ: {three_dimensions}')
    report = {'blender_version': bpy.app.version_string,
              'component_boundary_fallbacks': job['analysis'].get('component_boundary_fallbacks', []),
              'material_conflicts': job['analysis'].get('material_conflicts', []),
              'dimensions_cm': {'width': dimensions[0] * 100,
                                'thickness': dimensions[2] * 100,
                                'height': dimensions[1] * 100},
              'glb_reimport_dimensions_cm': [d * 100 for d in measured],
              'three_dimensions_cm': {'width': three_dimensions[0] * 100,
                                      'thickness': three_dimensions[1] * 100,
                                      'height': three_dimensions[2] * 100},
              'non_manifold_edges': non_manifold,
              'non_manifold_vertices': invalid_vertices,
              'topology_checks': topology_checks,
              'triangulation': job['analysis'].get('triangulation', []),
              'silhouette_islands': job['silhouette_islands'],
              'source_geometry_resolution': job['analysis']['source_geometry_resolution'],
              'mesh_resolution': job['analysis']['mesh_resolution'],
              'palette': job.get('palette', []),
              'mode': job['analysis'].get('mode', 'automatic-palette'),
              'color_fidelity': job['analysis'].get('color_fidelity', 'palette'),
              'color_warning': job['analysis'].get('color_warning'),
              'preview_color_mode': 'base-color-srgb',
              'geometry_mode': job['analysis'].get('geometry_mode', 'pixel-grid'),
              'boundary_mode': job['analysis'].get('boundary_mode', 'pixel-grid'),
              'boundary_network_mode': job['analysis'].get('boundary_network_mode'),
              'stroke_color_error_ratio': job['analysis'].get('stroke_color_error_ratio'),
              'curve_max_error_px': job['analysis'].get('curve_max_error_px'),
              'bezier_count': job['analysis'].get('bezier_count'),
              'curve_intersection_fallbacks': job['analysis'].get('curve_intersection_fallbacks'),
              'curve_smoothing_fallbacks': job['analysis'].get('curve_smoothing_fallbacks'),
              'removed_edge_matte_pixels': job['analysis'].get('removed_edge_matte_pixels', 0),
              'contour_source_resolution': job['analysis'].get('contour_source_resolution'),
              'contour_vertices': job['analysis'].get('contour_vertices'),
              'contour_tolerance_px': job['analysis'].get('contour_tolerance_px'),
              'contour_max_error_px': job['analysis'].get('contour_max_error_px'),
              'contour_holes': job['analysis'].get('contour_holes'),
              'material_count': len(gltf.get('materials', [])), 'texture_count': 0,
              'uv_count': 0, 'vertex_color_count': 0, 'glb_bytes': len(binary),
              'vertices': len(model.data.vertices), 'faces': len(model.data.polygons)}
    (root / 'validation.json').write_text(json.dumps(report, indent=2), encoding='utf-8')


if __name__ == '__main__':
    build_in_blender(Path(sys.argv[sys.argv.index('--blender-job') + 1]))
