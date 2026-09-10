# Ikiway POS Printer Agent

Agente local para imprimir comprobantes de `ikiway-commerce` directamente en una
impresora térmica USB compatible con ESC/POS. Imprime boletas de 58 u 80 mm y
convierte el timbre PDF417 del TED a raster ESC/POS sin deformarlo.

## Aplicación ejecutable para Windows

El proyecto incluye una interfaz Electron que ejecuta el agente en segundo plano,
permite elegir la impresora USB, guardar los dominios autorizados, imprimir una
prueba y activar el inicio automático con Windows.

Para compilar el instalador y la versión portable:

```bash
npm ci
npm run dist:win
```

También puedes hacer doble clic en `compilar_ikiway_pos_printer.bat`. Los dos
ejecutables quedan en `dist/`:

- `Ikiway POS Printer-Setup-2.0.0-x64.exe`
- `Ikiway POS Printer-Portable-2.0.0-x64.exe`

El instalador crea accesos directos. La aplicación permanece activa en el área de
notificación al cerrar la ventana. En el equipo de caja también debes instalar
[UsbDk](https://github.com/daynix/UsbDk/releases) para que Windows permita el
acceso directo a la impresora USB.

Los ejecutables generados localmente no llevan firma de código. Para distribución
externa conviene configurar un certificado de firma de Windows antes de compilar;
sin él, SmartScreen puede mostrar una advertencia al abrir el archivo.

La sección **Actividad y errores** muestra cada solicitud de impresión con fecha y
hora, número de venta, ticket, copias y resultado. Los eventos también se guardan
en `agent.jsonl` dentro de la carpeta de datos de la aplicación; el panel indica la
ruta exacta. El archivo rota automáticamente para no crecer sin límite y no guarda
el contenido completo de la boleta. Cada petición HTTP también queda registrada al
estilo Morgan, con método, ruta, código de respuesta y duración.

## Instalación desde el código fuente en Windows

1. Instala Node.js 20 o superior.
2. Instala [UsbDk](https://github.com/daynix/UsbDk/releases).
3. Ejecuta `instalar_tickeo_pos_printer.bat` una vez.
4. Copia `.env.example` como `.env` y agrega el origen exacto del POS a
   `ALLOWED_ORIGINS`. Por ejemplo: `https://pos.ikiway.cl`.
5. Ejecuta `iniciar_tickeo_pos_printer.bat`. Si hay más de una impresora USB,
   elige la correcta en la consola y deja la ventana abierta.
6. En Ikiway, entra a **Configuración TPV > Impresión** y selecciona
   **Agente USB Ikiway (PDF417)**.

El estado se puede consultar en <http://127.0.0.1:17891/health>. Debe mostrar
`"schema":"ikiway.receipt.v1"` y `"printer_connected":true`.

## Desarrollo

```bash
npm install
npm test
npm start
npm run desktop
```

Variables disponibles:

- `ALLOWED_ORIGINS`: orígenes autorizados separados por coma. Incluye esquema y
  puerto; no termina en `/`.
- `PRINTER_ENCODING`: codificación ESC/POS. Valor predeterminado: `cp850`.
- `PRINTER_CODE_PAGE`: número de tabla de caracteres ESC/POS. Predeterminado: `2`.

El servicio escucha únicamente en `127.0.0.1:17891`. Cada solicitud lleva un
identificador idempotente: recargar la página o consultar un envío dudoso no
duplica automáticamente el ticket. El botón **Reimprimir copia** sí genera una
nueva impresión después de confirmación.

## Integración con ikiway-commerce

Los archivos de la integración están en `integration/ikiway-commerce/`, con la
misma estructura del proyecto Django. Incluyen la migración `0012`, el contrato
del comprobante, el cliente del agente y la configuración del método de impresión.

Después de instalarlos en `ikiway-commerce`, ejecuta:

```bash
python manage.py migrate
python manage.py collectstatic --noinput
```

La impresión del navegador sigue disponible desde la vista del ticket. Si un DTE
no contiene TED o el PDF417 no cabe en el ancho configurado, Ikiway bloquea el
envío USB y muestra el motivo, para evitar imprimir una boleta electrónica sin su
timbre.
