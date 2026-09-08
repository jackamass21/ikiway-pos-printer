import "dotenv/config";
import readline from "node:readline/promises";
import USBAdapter, { sendToPrinter } from "./usb-adapter.mjs";
import { createApp } from "./app.mjs";

let selectedPrinter = null;
let selectedPrinterRef = null;

function log(level, message, meta) {
  const timestamp = new Date().toISOString();
  if (meta === undefined) {
    console[level](`[${timestamp}] ${message}`);
    return;
  }

  console[level](`[${timestamp}] ${message}`, meta);
}

function serializeError(error) {
  if (error instanceof Error) {
    return {
      name: error.name,
      message: error.message,
      stack: error.stack
    };
  }

  return { error: String(error) };
}

function toHex(value, width = 4) {
  return `0x${Number(value ?? 0).toString(16).padStart(width, "0")}`;
}

async function getStringDescriptorSafe(device, index) {
  if (!index) return null;

  return new Promise((resolve) => {
    device.getStringDescriptor(index, (error, value) => {
      if (error) {
        resolve(null);
        return;
      }

      resolve(typeof value === "string" ? value : String(value));
    });
  });
}

async function getPrinterUsbNames(device) {
  let openedHere = false;

  try {
    if (!device.interfaces) {
      device.open();
      openedHere = true;
    }

    const manufacturer = await getStringDescriptorSafe(device, device.deviceDescriptor?.iManufacturer);
    const product = await getStringDescriptorSafe(device, device.deviceDescriptor?.iProduct);

    return {
      manufacturer,
      product
    };
  } catch {
    return {
      manufacturer: null,
      product: null
    };
  } finally {
    if (openedHere) {
      try {
        device.close();
      } catch {}
    }
  }
}

async function describePrinter(device, index) {
  const names = await getPrinterUsbNames(device);

  return {
    index,
    manufacturer: names.manufacturer,
    product: names.product,
    vendorId: toHex(device.deviceDescriptor?.idVendor),
    productId: toHex(device.deviceDescriptor?.idProduct),
    busNumber: device.busNumber ?? null,
    deviceAddress: device.deviceAddress ?? null,
    portNumbers: Array.isArray(device.portNumbers) ? device.portNumbers.join(".") : null
  };
}

function getAvailablePrinters() {
  return USBAdapter.findPrinter();
}

function buildPrinterRef(device) {
  return {
    vendorId: device.deviceDescriptor?.idVendor ?? null,
    productId: device.deviceDescriptor?.idProduct ?? null,
    busNumber: device.busNumber ?? null,
    deviceAddress: device.deviceAddress ?? null,
    portNumbers: Array.isArray(device.portNumbers) ? device.portNumbers.join(".") : null
  };
}

function isSamePrinter(device, ref) {
  if (!ref) return false;

  return (
    device.deviceDescriptor?.idVendor === ref.vendorId &&
    device.deviceDescriptor?.idProduct === ref.productId &&
    device.busNumber === ref.busNumber &&
    (ref.portNumbers ? device.portNumbers?.join(".") === ref.portNumbers : device.deviceAddress === ref.deviceAddress)
  );
}

function getSelectedPrinterDevice() {
  const printers = getAvailablePrinters();

  if (!printers.length) {
    return null;
  }

  if (!selectedPrinterRef) {
    return printers.length === 1 ? printers[0] : null;
  }

  return printers.find((device) => isSamePrinter(device, selectedPrinterRef)) ?? null;
}

async function choosePrinterInteractively(printers) {
  if (!process.stdin.isTTY || !process.stdout.isTTY) {
    throw new Error("Hay varias impresoras. Inicia el agente en una terminal y selecciona una.");
  }

  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout
  });

  try {
    const answer = await rl.question(`Selecciona impresora [1-${printers.length}] (default 1): `);
    const chosenIndex = Number.parseInt(answer.trim() || "1", 10);

    if (!Number.isInteger(chosenIndex) || chosenIndex < 1 || chosenIndex > printers.length) {
      log("warn", "Seleccion invalida, se usara la impresora 1");
      return printers[0];
    }

    return printers[chosenIndex - 1];
  } finally {
    rl.close();
  }
}

async function initializePrinterSelection() {
  const printers = getAvailablePrinters();

  if (!printers.length) {
    log("warn", "No se detectaron impresoras USB compatibles al iniciar");
    return;
  }

  const describedPrinters = await Promise.all(
    printers.map((device, index) => describePrinter(device, index + 1))
  );

  log("info", "Impresoras USB detectadas", describedPrinters);

  if (printers.length === 1) {
    selectedPrinter = printers[0];
    selectedPrinterRef = buildPrinterRef(printers[0]);
    log("info", "Se selecciono automaticamente la unica impresora disponible", describedPrinters[0]);
    return;
  }

  selectedPrinter = await choosePrinterInteractively(printers);

  if (selectedPrinter) {
    selectedPrinterRef = buildPrinterRef(selectedPrinter);
    const index = printers.indexOf(selectedPrinter) + 1;
    log("info", "Impresora seleccionada", describedPrinters[index - 1] ?? await describePrinter(selectedPrinter, index));
  }
}


const app = createApp({
  allowedOrigins: (process.env.ALLOWED_ORIGINS || "http://localhost:8000,http://127.0.0.1:8000").split(",").map((value) => value.trim()).filter(Boolean),
  printerOptions: { encoding: process.env.PRINTER_ENCODING || "cp850", codePage: Number(process.env.PRINTER_CODE_PAGE || 2) },
  status: () => ({ printer_connected: Boolean(getSelectedPrinterDevice()), selected_printer: selectedPrinterRef }),
  print: async (buffer, copies) => {
    const device = getSelectedPrinterDevice();
    if (!device) throw new Error("No se encuentra la impresora USB seleccionada");
    await sendToPrinter(new USBAdapter(device), buffer, copies);
  },
});
await initializePrinterSelection();
app.listen(17891, "127.0.0.1", () => {
  console.log("Ikiway POS Printer Agent: http://127.0.0.1:17891/health");
});
