import assert from "node:assert/strict";
import { mkdtemp } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { LogStore } from "../electron/log-store.mjs";

test("persiste logs con fecha y devuelve primero el más reciente", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "ikiway-logs-"));
  const store = new LogStore(path.join(directory, "agent.jsonl"));
  await store.append({ level: "info", event: "print.received", message: "Solicitud recibida", meta: { orderId: 12 } });
  await store.append({ level: "error", event: "print.error", message: "Sin papel\nrevisar", meta: { ticket: "POS-12" } });

  const entries = await store.list();
  assert.equal(entries.length, 2);
  assert.equal(entries[0].event, "print.error");
  assert.equal(entries[0].message, "Sin papel revisar");
  assert.equal(entries[1].orderId, "12");
  assert.match(entries[0].timestamp, /^\d{4}-\d{2}-\d{2}T/);
});
