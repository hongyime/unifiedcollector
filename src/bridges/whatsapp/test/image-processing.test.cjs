const assert = require('node:assert/strict');
const test = require('node:test');
const sharp = require('sharp');

for (const format of ['jpeg', 'png', 'avif']) {
  test(`native ${format} image processing survives a resize round trip`, async () => {
    const input = await sharp({ create: {
      width: 16, height: 12, channels: 3,
      background: { r: 240, g: 80, b: 60 },
    } }).toFormat(format).toBuffer();
    const output = await sharp(input).resize(8, 6).png().toBuffer();
    const metadata = await sharp(output).metadata();
    assert.equal(metadata.format, 'png');
    assert.equal(metadata.width, 8);
    assert.equal(metadata.height, 6);
  });
}

test('invalid image bytes reject without returning an image', async () => {
  await assert.rejects(sharp(Buffer.from('synthetic invalid image')).png().toBuffer());
});
