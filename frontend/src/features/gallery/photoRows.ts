import type { AssetGalleryItem } from "../../api/types";

export function photoRows(items: AssetGalleryItem[], width: number, target: number) {
  const rows: Array<{ items: AssetGalleryItem[]; height: number; widths: number[] }> = [];
  let pending: AssetGalleryItem[] = [];
  let ratios: number[] = [];
  const flush = (last: boolean) => {
    if (!pending.length) return;
    const available = Math.max(1, width - (pending.length - 1) * 12);
    const natural = available / ratios.reduce((total, ratio) => total + ratio, 0);
    const height = last ? Math.min(target, natural) : natural;
    rows.push({ items: pending, height, widths: ratios.map((ratio) => ratio * height) });
    pending = []; ratios = [];
  };
  for (const item of items) {
    pending.push(item);
    const ratio = item.width && item.height ? item.width / item.height : 1.5;
    ratios.push(Math.max(.25, Math.min(4, ratio)));
    if (ratios.reduce((total, value) => total + value, 0) * target + (pending.length - 1) * 12 >= width) flush(false);
  }
  flush(true);
  return rows;
}
