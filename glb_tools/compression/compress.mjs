import fs from 'node:fs/promises';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import MeshoptEncoder from 'meshoptimizer/meshopt_encoder.js';
import { MeshoptDecoder } from 'three/addons/libs/meshopt_decoder.module.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { Box3 } from 'three';

const extension = 'EXT_meshopt_compression';
const hash = data => createHash('sha256').update(data).digest('hex');

export function readGlb(bytes) {
  assert(bytes.length >= 28, 'GLB header is incomplete');
  assert.equal(bytes.readUInt32LE(0), 0x46546c67);
  assert.equal(bytes.readUInt32LE(4), 2);
  assert.equal(bytes.readUInt32LE(8), bytes.length);
  const length = bytes.readUInt32LE(12), offset = 20 + length;
  assert.equal(bytes.readUInt32LE(16), 0x4e4f534a);
  assert(offset + 8 <= bytes.length);
  assert.equal(bytes.readUInt32LE(offset + 4), 0x004e4942);
  assert.equal(offset + 8 + bytes.readUInt32LE(offset), bytes.length);
  return { json: JSON.parse(bytes.subarray(20, offset).toString()), bin: bytes.subarray(offset + 8) };
}

export function writeGlb(json, bin) {
  const text = Buffer.from(JSON.stringify(json));
  const paddedJson = Buffer.alloc((text.length + 3) & ~3, 0x20);
  text.copy(paddedJson);
  const paddedBin = Buffer.alloc((bin.length + 3) & ~3);
  bin.copy(paddedBin);
  const output = Buffer.alloc(28 + paddedJson.length + paddedBin.length);
  [0x46546c67, 2, output.length, paddedJson.length, 0x4e4f534a].forEach((v, i) => output.writeUInt32LE(v, i * 4));
  paddedJson.copy(output, 20);
  output.writeUInt32LE(paddedBin.length, 20 + paddedJson.length);
  output.writeUInt32LE(0x004e4942, 24 + paddedJson.length);
  paddedBin.copy(output, 28 + paddedJson.length);
  return output;
}

function semantics(json) {
  const result = structuredClone(json);
  delete result.buffers;
  delete result.bufferViews;
  for (const key of ['extensionsUsed', 'extensionsRequired']) {
    if (result[key]) result[key] = result[key].filter(v => v !== extension);
    if (!result[key]?.length) delete result[key];
  }
  return result;
}

async function verifyLoader(original, compressed) {
  const loader = new GLTFLoader().setMeshoptDecoder(MeshoptDecoder);
  const buffer = b => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength);
  const before = await loader.parseAsync(buffer(original), '');
  const after = await loader.parseAsync(buffer(compressed), '');
  const meshes = scene => { const list = []; scene.traverse(o => { if (o.isMesh) list.push(o); }); return list; };
  const left = meshes(before.scene), right = meshes(after.scene);
  assert.equal(left.length, right.length);
  let vertices = 0, triangles = 0;
  for (let i = 0; i < left.length; i++) {
    const a = left[i], b = right[i];
    assert.deepEqual(Object.keys(a.geometry.attributes), Object.keys(b.geometry.attributes));
    for (const key of Object.keys(a.geometry.attributes)) assert.deepEqual(a.geometry.attributes[key].array, b.geometry.attributes[key].array);
    assert.deepEqual(a.geometry.index?.array, b.geometry.index?.array);
    assert.deepEqual(a.matrixWorld.elements, b.matrixWorld.elements);
    vertices += a.geometry.attributes.position.count;
    triangles += (a.geometry.index?.count ?? a.geometry.attributes.position.count) / 3;
  }
  const box = scene => { const b = new Box3().setFromObject(scene); return { min: b.min.toArray(), max: b.max.toArray() }; };
  assert.deepEqual(box(before.scene), box(after.scene));
  return { meshes: left.length, vertices, triangles, bounds: box(after.scene), exact: true };
}

export async function compress(original) {
  await Promise.all([MeshoptEncoder.ready, MeshoptDecoder.ready]);
  const { json, bin } = readGlb(original);
  assert.equal(json.buffers.length, 1, 'Only a single embedded buffer is supported');
  assert(!json.buffers[0].uri && !json.images?.length && !json.skins?.length && !json.animations?.length, 'Only generated texture-free static GLB is supported');
  assert(!json.extensionsUsed?.includes(extension), 'Input is already compressed');
  const output = structuredClone(json), chunks = [], checks = [];
  let offset = 0;
  for (let i = 0; i < json.bufferViews.length; i++) {
    const view = json.bufferViews[i];
    const accessors = json.accessors.filter(a => a.bufferView === i);
    assert.equal(accessors.length, 1, 'Shared buffer views are not supported');
    const accessor = accessors[0];
    assert(!accessor.sparse && !accessor.byteOffset && !view.extensions);
    const indices = accessor.type === 'SCALAR';
    assert(indices ? [5123, 5125].includes(accessor.componentType) : accessor.type === 'VEC3' && accessor.componentType === 5126);
    const stride = indices ? (accessor.componentType === 5123 ? 2 : 4) : 12;
    assert(!view.byteStride || view.byteStride === stride);
    assert.equal(view.byteLength, accessor.count * stride);
    const start = view.byteOffset || 0;
    assert(start + view.byteLength <= json.buffers[0].byteLength);
    const raw = bin.subarray(start, start + view.byteLength);
    const mode = indices ? 'INDICES' : 'ATTRIBUTES';
    const encoded = Buffer.from(MeshoptEncoder.encodeGltfBuffer(raw, accessor.count, stride, mode));
    const decoded = new Uint8Array(raw.length);
    MeshoptDecoder.decodeGltfBuffer(decoded, accessor.count, stride, encoded, mode, 'NONE');
    assert.deepEqual(Buffer.from(decoded), raw);
    output.bufferViews[i].buffer = 1;
    output.bufferViews[i].extensions = { [extension]: { buffer: 0, byteOffset: offset, byteLength: encoded.length, byteStride: stride, count: accessor.count, mode, filter: 'NONE' } };
    chunks.push(encoded);
    offset += encoded.length;
    const padding = (4 - offset % 4) % 4;
    chunks.push(Buffer.alloc(padding)); offset += padding;
    checks.push({ view: i, original_sha256: hash(raw), decoded_sha256: hash(decoded), exact: true });
  }
  output.buffers = [{ byteLength: offset }, { byteLength: json.buffers[0].byteLength, extensions: { [extension]: { fallback: true } } }];
  output.extensionsUsed = [...new Set([...(json.extensionsUsed || []), extension])];
  output.extensionsRequired = [...new Set([...(json.extensionsRequired || []), extension])];
  const compressed = writeGlb(output, Buffer.concat(chunks));
  const reread = readGlb(compressed);
  assert.deepEqual(semantics(json), semantics(reread.json));
  const restored = Buffer.alloc(bin.length);
  for (let i = 0; i < reread.json.bufferViews.length; i++) {
    const view = reread.json.bufferViews[i], e = view.extensions[extension];
    const decoded = new Uint8Array(view.byteLength);
    MeshoptDecoder.decodeGltfBuffer(decoded, e.count, e.byteStride, reread.bin.subarray(e.byteOffset, e.byteOffset + e.byteLength), e.mode, e.filter);
    assert.equal(hash(decoded), checks[i].original_sha256);
    Buffer.from(decoded).copy(restored, view.byteOffset || 0);
  }
  assert.deepEqual(restored, bin);
  const loader = await verifyLoader(original, compressed);
  return { compressed, report: { codec: 'meshoptimizer@0.22.0', extension, lossless: true,
    quantization: false, reorder: false, filter: 'NONE', original_bytes: original.length,
    compressed_bytes: compressed.length, saved_percent: 100 * (1 - compressed.length / original.length),
    original_sha256: hash(original), compressed_sha256: hash(compressed), buffer_checks: checks, loader } };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const [input, output, reportPath] = process.argv.slice(2);
    if (!input || !output || !reportPath) throw new Error('Usage: compress.mjs input.glb output.glb report.json');
    const result = await compress(await fs.readFile(input));
    await fs.writeFile(output, result.compressed, { flag: 'wx' });
    await fs.writeFile(reportPath, JSON.stringify(result.report, null, 2), { flag: 'wx' });
    process.stdout.write('Meshopt byte-exact verification passed\n');
  } catch (error) {
    process.stderr.write(error.message + '\n');
    process.exitCode = 1;
  }
}
