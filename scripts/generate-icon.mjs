import { mkdir, writeFile } from "node:fs/promises";
import { PNG } from "pngjs";

const size = 512;
const png = new PNG({ width: size, height: size });

function pixel(x, y, color) {
  if (x < 0 || y < 0 || x >= size || y >= size) return;
  const offset = (y * size + x) * 4;
  [png.data[offset], png.data[offset + 1], png.data[offset + 2], png.data[offset + 3]] = color;
}

function roundedRect(x, y, width, height, radius, color) {
  for (let py = y; py < y + height; py++) {
    for (let px = x; px < x + width; px++) {
      const dx = Math.max(x + radius - px, 0, px - (x + width - radius - 1));
      const dy = Math.max(y + radius - py, 0, py - (y + height - radius - 1));
      if (dx * dx + dy * dy <= radius * radius) pixel(px, py, color);
    }
  }
}

const blue = [29, 79, 232, 255];
const white = [255, 255, 255, 255];
roundedRect(0, 0, size, size, 92, blue);
roundedRect(112, 105, 288, 190, 24, white);
roundedRect(82, 218, 348, 172, 30, white);
roundedRect(132, 280, 248, 155, 12, blue);
roundedRect(153, 301, 206, 114, 8, white);
roundedRect(341, 246, 42, 22, 8, blue);

await mkdir("build", { recursive: true });
await writeFile("build/icon.png", PNG.sync.write(png));
console.log("Icono generado en build/icon.png");
