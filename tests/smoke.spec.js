const { test, expect } = require('@playwright/test');
const path = require('node:path');
const fs = require('node:fs/promises');

test('descarga el Excel desde Estado CNSF', async ({ page }) => {
  const downloadDir = path.join(process.cwd(), 'downloads');
  await fs.mkdir(downloadDir, { recursive: true });

  await page.goto('https://sio.cnsf.gob.mx/Estado');
  await page.waitForLoadState('domcontentloaded');

  const descargaLink = page.getByRole('link', { name: 'Descargar Base Completa' });
  await expect(descargaLink).toBeVisible({ timeout: 15000 });

  const downloadPromise = page.waitForEvent('download');
  await descargaLink.click();
  const download = await downloadPromise;

  const fileName = download.suggestedFilename();
  const targetPath = path.join(downloadDir, fileName);

  await download.saveAs(targetPath);

  expect(await download.failure()).toBeNull();

  const stat = await fs.stat(targetPath);
  expect(stat.isFile()).toBeTruthy();
});