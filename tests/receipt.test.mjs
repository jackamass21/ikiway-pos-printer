import assert from "node:assert/strict";
import test from "node:test";
import { buildReceipt, PayloadError, pdf417Raster } from "../receipt.mjs";
import { barcodeDataUri, receipt } from "./fixtures.mjs";

test("genera un ticket ESC/POS de Ikiway con raster PDF417 y corte", () => {
  const data = buildReceipt(receipt());
  assert.deepEqual(data.subarray(0, 5), Buffer.from([0x1b, 0x40, 0x1b, 0x74, 2]));
  assert.notEqual(data.indexOf(Buffer.from([0x1d, 0x76, 0x30, 0])), -1);
  assert.deepEqual(data.subarray(-5), Buffer.from([0x1b, 0x64, 4, 0x1d, 0x56, 1]).subarray(1));
  assert.match(data.toString("latin1"), /IKIWAY SPA/);
});

test("convierte el PNG a filas de bits sin cambiar sus dimensiones", () => {
  const raster = pdf417Raster(barcodeDataUri(65, 20), 384);
  assert.deepEqual([...raster.subarray(0, 8)], [0x1d, 0x76, 0x30, 0, 9, 0, 20, 0]);
  assert.equal(raster.length, 8 + 9 * 20);
});

test("rechaza un DTE electrónico sin PDF417 antes de imprimir", () => {
  const payload = receipt();
  payload.electronic_document.pdf417_data_uri = "";
  assert.throws(() => buildReceipt(payload), PayloadError);
});

test("rechaza un PDF417 más ancho que el cabezal de 58 mm", () => {
  const payload = receipt();
  payload.config.paper_width = "58";
  payload.electronic_document.pdf417_data_uri = barcodeDataUri(385, 20);
  assert.throws(() => buildReceipt(payload), /excede 384 puntos/);
});
