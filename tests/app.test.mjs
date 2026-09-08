import assert from "node:assert/strict";
import test from "node:test";
import { createApp } from "../app.mjs";
import { receipt } from "./fixtures.mjs";

async function withServer(callback, print = async () => {}) {
  const app = createApp({ print, status: () => ({ printer_connected: true }), allowedOrigins: ["https://pos.ikiway.cl"] });
  const server = await new Promise((resolve) => {
    const instance = app.listen(0, "127.0.0.1", () => resolve(instance));
  });
  try {
    await callback(`http://127.0.0.1:${server.address().port}`);
  } finally {
    await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
  }
}

test("health expone el contrato compatible", async () => {
  await withServer(async (base) => {
    const response = await fetch(`${base}/health`, { headers: { Origin: "https://pos.ikiway.cl" } });
    assert.equal(response.status, 200);
    const body = await response.json();
    assert.equal(body.schema, "ikiway.receipt.v1");
    assert.equal(body.printer_connected, true);
  });
});

test("un job repetido se imprime una sola vez", async () => {
  let calls = 0;
  await withServer(async (base) => {
    const options = {
      method: "POST",
      headers: { Origin: "https://pos.ikiway.cl", "Content-Type": "application/json", "X-Print-Job-Id": "1234567890abcdef" },
      body: JSON.stringify(receipt()),
    };
    const first = await fetch(`${base}/print`, options);
    const second = await fetch(`${base}/print`, options);
    assert.equal(first.status, 200);
    assert.equal(second.status, 200);
    assert.equal(calls, 1);
  }, async () => { calls += 1; });
});

test("rechaza origen no configurado", async () => {
  await withServer(async (base) => {
    const response = await fetch(`${base}/health`, { headers: { Origin: "https://otro.example" } });
    assert.equal(response.status, 403);
  });
});
