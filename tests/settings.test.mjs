import assert from "node:assert/strict";
import { mkdtemp, readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { normalizeOrigins, SettingsStore } from "../electron/settings.mjs";

test("normaliza orígenes y elimina duplicados", () => {
  assert.deepEqual(
    normalizeOrigins("https://pos.ikiway.cl/\nhttp://localhost:8000,https://pos.ikiway.cl"),
    ["https://pos.ikiway.cl", "http://localhost:8000"],
  );
});

test("rechaza rutas y protocolos distintos de HTTP", () => {
  assert.throws(() => normalizeOrigins("https://pos.ikiway.cl/admin"), /sólo esquema/);
  assert.throws(() => normalizeOrigins("file:///tmp/pos"), /Origen inválido/);
});

test("guarda configuración de forma persistente", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "ikiway-settings-"));
  const file = path.join(directory, "settings.json");
  const store = new SettingsStore(file, { allowedOrigins: ["http://localhost:8000"] });
  await store.update({
    allowedOrigins: ["https://pos.ikiway.cl"],
    selectedPrinter: { vendorId: 1208, productId: 514, portNumbers: "1.2" },
  });

  const saved = JSON.parse(await readFile(file, "utf8"));
  assert.deepEqual(saved.allowedOrigins, ["https://pos.ikiway.cl"]);
  assert.equal(saved.selectedPrinter.portNumbers, "1.2");
});
