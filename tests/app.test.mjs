import assert from "node:assert/strict";
import test from "node:test";
import { createApp } from "../app.mjs";
import { receipt } from "./fixtures.mjs";

async function withServer(callback, { print = async () => {}, logEvent = () => {} } = {}) {
  const app = createApp({ print, logEvent, status: () => ({ printer_connected: true }), allowedOrigins: ["https://pos.ikiway.cl"] });
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
  const events = [];
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
    assert.equal(events.filter((entry) => entry.event === "print.received").length, 2);
    assert.equal(events.filter((entry) => entry.event === "print.success").length, 1);
  }, {
    print: async () => { calls += 1; },
    logEvent: (entry) => events.push(entry),
  });
});

test("rechaza origen no configurado", async () => {
  await withServer(async (base) => {
    const response = await fetch(`${base}/health`, { headers: { Origin: "https://otro.example" } });
    assert.equal(response.status, 403);
  });
});

test("registra fecha contextual y error cuando falla el USB", async () => {
  const events = [];
  await withServer(async (base) => {
    const response = await fetch(`${base}/print`, {
      method: "POST",
      headers: { Origin: "https://pos.ikiway.cl", "Content-Type": "application/json", "X-Print-Job-Id": "error12345678901" },
      body: JSON.stringify(receipt()),
    });
    assert.equal(response.status, 502);
    const error = events.find((entry) => entry.event === "print.error");
    assert.equal(error.meta.orderId, 42);
    assert.equal(error.meta.ticket, "POS-000042");
    assert.match(error.message, /Sin papel/);
  }, {
    print: async () => { throw new Error("Sin papel"); },
    logEvent: (entry) => events.push(entry),
  });
});
