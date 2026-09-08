import readline from "node:readline/promises";
import USBAdapter, { sendToPrinter } from "./usb-adapter.mjs";

function toHex(value, width = 4) {
  return `0x${Number(value ?? 0).toString(16).padStart(width, "0")}`;
}

function buildPrinterRef(device) {
  return {
    vendorId: device.deviceDescriptor?.idVendor ?? null,
    productId: device.deviceDescriptor?.idProduct ?? null,
    busNumber: device.busNumber ?? null,
    deviceAddress: device.deviceAddress ?? null,
    portNumbers: Array.isArray(device.portNumbers) ? device.portNumbers.join(".") : null,
  };
}

function printerKey(device) {
  const ref = buildPrinterRef(device);
  return [ref.vendorId, ref.productId, ref.portNumbers || `${ref.busNumber}:${ref.deviceAddress}`].join(":");
}

function isSamePrinter(device, ref) {
  if (!ref) return false;
  if (
    device.deviceDescriptor?.idVendor !== ref.vendorId ||
    device.deviceDescriptor?.idProduct !== ref.productId
  ) return false;
  if (ref.portNumbers) return device.portNumbers?.join(".") === ref.portNumbers;
  return device.busNumber === ref.busNumber && device.deviceAddress === ref.deviceAddress;
}

async function getStringDescriptor(device, index) {
  if (!index) return null;
  return new Promise((resolve) => {
    device.getStringDescriptor(index, (error, value) => resolve(error ? null : String(value)));
  });
}

async function usbNames(device) {
  let openedHere = false;
  try {
    if (!device.interfaces) {
      device.open();
      openedHere = true;
    }
    const [manufacturer, product] = await Promise.all([
      getStringDescriptor(device, device.deviceDescriptor?.iManufacturer),
      getStringDescriptor(device, device.deviceDescriptor?.iProduct),
    ]);
    return { manufacturer, product };
  } catch {
    return { manufacturer: null, product: null };
  } finally {
    if (openedHere) {
      try { device.close(); } catch {}
    }
  }
}

export class PrinterManager {
  constructor({ selectedRef = null, onSelectionChange = () => {} } = {}) {
    this.selectedRef = selectedRef;
    this.onSelectionChange = onSelectionChange;
  }

  devices() {
    return USBAdapter.findPrinter();
  }

  selectedDevice() {
    const devices = this.devices();
    if (!devices.length) return null;
    if (this.selectedRef) return devices.find((device) => isSamePrinter(device, this.selectedRef)) ?? null;
    return devices.length === 1 ? devices[0] : null;
  }

  status() {
    return {
      printer_connected: Boolean(this.selectedDevice()),
      selected_printer: this.selectedRef,
    };
  }

  async list() {
    const devices = this.devices();
    return Promise.all(devices.map(async (device, index) => {
      const names = await usbNames(device);
      const ref = buildPrinterRef(device);
      return {
        key: printerKey(device),
        index: index + 1,
        manufacturer: names.manufacturer,
        product: names.product,
        vendorId: toHex(ref.vendorId),
        productId: toHex(ref.productId),
        portNumbers: ref.portNumbers,
        selected: isSamePrinter(device, this.selectedRef) || (!this.selectedRef && devices.length === 1),
      };
    }));
  }

  async select(key) {
    const device = this.devices().find((candidate) => printerKey(candidate) === key);
    if (!device) throw new Error("La impresora ya no está conectada.");
    this.selectedRef = buildPrinterRef(device);
    await this.onSelectionChange(this.selectedRef);
    return this.status();
  }

  async initialize({ interactive = false } = {}) {
    if (this.selectedDevice()) return this.status();
    const devices = this.devices();
    if (!devices.length) return this.status();
    if (devices.length === 1) {
      this.selectedRef = buildPrinterRef(devices[0]);
      await this.onSelectionChange(this.selectedRef);
      return this.status();
    }
    if (!interactive || !process.stdin.isTTY || !process.stdout.isTTY) return this.status();

    const printers = await this.list();
    for (const printer of printers) {
      console.log(`${printer.index}. ${printer.product || "Impresora USB"} (${printer.vendorId}:${printer.productId})`);
    }
    const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
    try {
      const answer = await rl.question(`Selecciona impresora [1-${printers.length}]: `);
      const index = Number.parseInt(answer.trim(), 10) - 1;
      if (!printers[index]) throw new Error("Selección de impresora inválida.");
      return this.select(printers[index].key);
    } finally {
      rl.close();
    }
  }

  async print(buffer, copies = 1) {
    const device = this.selectedDevice();
    if (!device) throw new Error("No se encuentra la impresora USB seleccionada.");
    await sendToPrinter(new USBAdapter(device), buffer, copies);
  }
}
