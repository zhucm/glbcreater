import test from 'node:test';
import assert from 'node:assert/strict';
import { compress, readGlb, writeGlb } from '../glb_tools/compression/compress.mjs';

function triangle() {
  const bin = Buffer.alloc(80);
  [0, 0, 0, 1, 0, 0, 0, 0, 1].forEach((v, i) => bin.writeFloatLE(v, i * 4));
  [0, 1, 0, 0, 1, 0, 0, 1, 0].forEach((v, i) => bin.writeFloatLE(v, 36 + i * 4));
  [0, 1, 2].forEach((v, i) => bin.writeUInt16LE(v, 72 + i * 2));
  return writeGlb({ asset: { version: '2.0' }, buffers: [{ byteLength: 80 }],
    bufferViews: [{ buffer: 0, byteOffset: 0, byteLength: 36, target: 34962 }, { buffer: 0, byteOffset: 36, byteLength: 36, target: 34962 }, { buffer: 0, byteOffset: 72, byteLength: 6, target: 34963 }],
    accessors: [{ bufferView: 0, componentType: 5126, count: 3, type: 'VEC3', min: [0, 0, 0], max: [1, 0, 1] }, { bufferView: 1, componentType: 5126, count: 3, type: 'VEC3' }, { bufferView: 2, componentType: 5123, count: 3, type: 'SCALAR' }],
    materials: [{ pbrMetallicRoughness: { baseColorFactor: [.9, .2, .1, 1], metallicFactor: 0, roughnessFactor: .7 } }],
    meshes: [{ primitives: [{ attributes: { POSITION: 0, NORMAL: 1 }, indices: 2, material: 0 }] }],
    nodes: [{ mesh: 0 }], scenes: [{ nodes: [0] }], scene: 0 }, bin);
}

test('Meshopt preserves position, normals, indices, materials and bounds', async () => {
  const original = triangle();
  const { compressed, report } = await compress(original);
  assert.equal(report.lossless, true);
  assert.equal(report.loader.triangles, 1);
  assert.equal(report.buffer_checks.length, 3);
  report.buffer_checks.forEach(c => assert.equal(c.original_sha256, c.decoded_sha256));
  assert.deepEqual(readGlb(original).json.materials, readGlb(compressed).json.materials);
  await assert.rejects(compress(compressed));
});

test('Unsupported or invalid GLB is rejected', async () => {
  assert.throws(() => readGlb(Buffer.from('bad')));
  const { json, bin } = readGlb(triangle());
  json.images = [{ uri: 'external.png' }];
  await assert.rejects(compress(writeGlb(json, bin)));
});
