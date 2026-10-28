import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import { ProductExportError, productExport } from "../../domain/productExport";
import ProductExportPanel from "./ProductExportPanel";

const options = { scopes: [{ id: 'reading', label: 'Reading history' }, { id: 'conversations', label: 'Ask Ryn history' }],
  excluded: ['Private keys and invite secrets', 'Other devices and browsers'] };
afterEach(() => vi.restoreAllMocks());

it('downloads only explicitly selected scopes and reports a requested browser download', async () => {
  vi.spyOn(productExport, 'options').mockResolvedValue(options);
  const blob = new Blob(['zip'], { type: 'application/zip' });
  const download = vi.spyOn(productExport, 'download').mockResolvedValue(blob);
  const save = vi.spyOn(productExport, 'save').mockImplementation(() => {});
  const user = userEvent.setup();
  render(<ProductExportPanel />);
  await user.click(await screen.findByRole('checkbox', { name: 'Ask Ryn history' }));
  expect(download).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: 'Download selected data (ZIP)' }));
  expect(download).toHaveBeenCalledExactlyOnceWith(['reading'], expect.any(AbortSignal));
  expect(save).toHaveBeenCalledExactlyOnceWith(blob);
  expect(screen.getByRole('status')).toHaveTextContent('download requested');
  expect(screen.getByRole('status')).toHaveFocus();
});

it('keeps the selected scope after a source failure and offers retry without false success', async () => {
  vi.spyOn(productExport, 'options').mockResolvedValue(options);
  vi.spyOn(productExport, 'download').mockRejectedValue(new ProductExportError('privacy_export_source_unavailable', 'reading'));
  const save = vi.spyOn(productExport, 'save');
  const user = userEvent.setup();
  render(<ProductExportPanel />);
  await user.click(await screen.findByRole('button', { name: 'Download selected data (ZIP)' }));
  expect(screen.getByRole('alert')).toHaveTextContent('Reading history: The export could not be completed');
  expect(screen.getByRole('alert')).toHaveFocus();
  expect(save).not.toHaveBeenCalled();
  expect(screen.getByRole('checkbox', { name: 'Reading history' })).toBeChecked();
  expect(screen.getByRole('button', { name: 'Download selected data (ZIP)' })).toBeEnabled();
});

it('disables an empty selection and duplicate clicks while an export is running', async () => {
  vi.spyOn(productExport, 'options').mockResolvedValue(options);
  let finish: (blob: Blob) => void = () => {};
  const download = vi.spyOn(productExport, 'download').mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
  vi.spyOn(productExport, 'save').mockImplementation(() => {});
  const user = userEvent.setup();
  render(<ProductExportPanel />);
  await user.click(await screen.findByRole('checkbox', { name: 'Reading history' }));
  await user.click(screen.getByRole('checkbox', { name: 'Ask Ryn history' }));
  expect(screen.getByRole('button', { name: 'Download selected data (ZIP)' })).toBeDisabled();
  await user.click(screen.getByRole('checkbox', { name: 'Reading history' }));
  await user.click(screen.getByRole('button', { name: 'Download selected data (ZIP)' }));
  await user.click(screen.getByRole('button', { name: 'Preparing export…' }));
  expect(download).toHaveBeenCalledTimes(1);
  expect(screen.getByRole('checkbox', { name: 'Reading history' })).toBeDisabled();
  await act(async () => finish(new Blob()));
  expect(screen.getByRole('button', { name: 'Download selected data (ZIP)' })).toBeEnabled();
});

it('can retry loading scope choices and aborts its request when leaving the page', async () => {
  const load = vi.spyOn(productExport, 'options').mockRejectedValueOnce(new Error('network')).mockResolvedValue(options);
  const user = userEvent.setup();
  const view = render(<ProductExportPanel />);
  await user.click(await screen.findByRole('button', { name: 'Retry export options' }));
  await screen.findByRole('checkbox', { name: 'Reading history' });
  view.unmount();
  expect(load.mock.calls[1][0]?.aborted).toBe(true);
});
