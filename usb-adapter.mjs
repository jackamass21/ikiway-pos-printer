import os from "node:os";
import { EventEmitter } from "node:events";
import usbModule from "usb";

const usb = usbModule.usb || usbModule;
if (os.platform() === "win32" && typeof usbModule.useUsbDkBackend === "function") {
  try {
    usbModule.useUsbDkBackend();
  } catch {
    // La apertura mostrara el error real si UsbDk no esta instalado.
  }
}

export default class USBAdapter extends EventEmitter {
  static findPrinter() {
    return usb.getDeviceList().filter((device) => {
      try {
        return device.configDescriptor.interfaces.some((iface) => iface.some((conf) => conf.bInterfaceClass === 7));
      } catch { return false; }
    });
  }

  constructor(device) {
    super();
    this.device = device;
    this.endpoint = null;
    this.iface = null;
    this.opened = false;
    this.detachedKernel = false;
  }

  open(callback) {
    try {
      if (!this.device) throw new Error("No hay impresora USB seleccionada");
      this.device.open();
      this.opened = true;
      this.iface = this.device.interfaces.find((iface) => iface.descriptor.bInterfaceClass === 7 && iface.endpoints.some((endpoint) => endpoint.direction === "out"));
      if (!this.iface) throw new Error("La impresora no tiene endpoint USB de salida");
      if (os.platform() === "linux" && this.iface.isKernelDriverActive()) {
        this.iface.detachKernelDriver();
        this.detachedKernel = true;
      }
      this.iface.claim();
      this.claimed = true;
      this.endpoint = this.iface.endpoints.find((endpoint) => endpoint.direction === "out");
      this.endpoint.timeout = 10000;
      callback?.(null);
    } catch (error) { callback?.(error); }
    return this;
  }

  write(data, callback) {
    if (!this.endpoint) { callback?.(new Error("USB no esta abierto")); return this; }
    // Limitar cada transferencia para impresoras con buffers USB pequenos.
    let offset = 0;
    const next = (error) => {
      if (error || offset >= data.length) { callback?.(error || null); return; }
      const chunk = data.subarray(offset, offset + 4096);
      offset += chunk.length;
      try { this.endpoint.transfer(chunk, next); } catch (err) { callback?.(err); }
    };
    next();
    return this;
  }

  close(callback) {
    const finish = (releaseError) => {
      let error = releaseError;
      try {
        if (this.detachedKernel) this.iface.attachKernelDriver();
      } catch (err) { error ||= err; }
      try { if (this.opened) this.device.close(); } catch (err) { error ||= err; }
      this.opened = false;
      this.claimed = false;
      this.endpoint = null;
      callback?.(error || null);
    };
    if (this.claimed) {
      try { this.iface.release(true, finish); } catch (err) { finish(err); }
    } else finish();
    return this;
  }
}

export async function sendToPrinter(device, buffer, copies = 1) {
  const invoke = (method, ...args) => new Promise((resolve, reject) => device[method](...args, (error) => error ? reject(error) : resolve()));
  let failure;
  try {
    await invoke("open");
    for (let i = 0; i < copies; i++) await invoke("write", buffer);
  } catch (error) { failure = error; }
  try { await invoke("close"); } catch (error) { failure ||= error; }
  if (failure) throw failure;
}
