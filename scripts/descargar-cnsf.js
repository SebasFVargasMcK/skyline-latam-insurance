const { chromium } = require('playwright');
const path = require('node:path');
const fs = require('node:fs/promises');

async function descargarCNSF() {
  const rawDir = path.join(process.cwd(), 'data', 'raw');
  await fs.mkdir(rawDir, { recursive: true });

  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext({ acceptDownloads: true });
  const page = await context.newPage();

  try {
    await page.goto('https://sio.cnsf.gob.mx/Estado', {
      waitUntil: 'domcontentloaded',
    });

    const descargaLink = page.getByRole('link', {
      name: 'Descargar Base Completa',
    });

    await descargaLink.waitFor({ state: 'visible', timeout: 15000 });

    const downloadPromise = page.waitForEvent('download');
    await descargaLink.click();
    const download = await downloadPromise;

    const targetPath = path.join(rawDir, 'estado_resultados_sio.xlsx');
    await download.saveAs(targetPath);

    console.log('Archivo descargado en:', targetPath);
  } catch (error) {
    console.error('Error al descargar:', error);
    process.exitCode = 1;
  } finally {
    await context.close();
    await browser.close();
  }
}

descargarCNSF();
