import type { DialogItem } from "../types";

const DRAFT_PREFIX = "foldesk_draft_";

export function draftKeyForDialog(dialog: Pick<DialogItem, "id" | "lead_id"> | null | undefined): string | null {
  if (!dialog) return null;
  if (dialog.lead_id != null && Number(dialog.lead_id) > 0) {
    return `${DRAFT_PREFIX}lead_${Number(dialog.lead_id)}`;
  }
  if (dialog.id != null && Number(dialog.id) > 0) {
    return `${DRAFT_PREFIX}dlg_${Number(dialog.id)}`;
  }
  return null;
}

export function readDraft(dialog: Pick<DialogItem, "id" | "lead_id"> | null | undefined): string {
  const key = draftKeyForDialog(dialog);
  if (!key) return "";
  try {
    return localStorage.getItem(key) || "";
  } catch {
    return "";
  }
}

export function writeDraft(dialog: Pick<DialogItem, "id" | "lead_id"> | null | undefined, text: string): void {
  const key = draftKeyForDialog(dialog);
  if (!key) return;
  try {
    if (text && text.trim()) {
      localStorage.setItem(key, text);
    } else {
      localStorage.removeItem(key);
    }
  } catch {
    /* ignore storage quota errors */
  }
}

export function hasDraft(dialog: Pick<DialogItem, "id" | "lead_id"> | null | undefined): boolean {
  return Boolean(readDraft(dialog).trim());
}

export function listDraftKeysSnapshot(): Set<string> {
  const out = new Set<string>();
  try {
    for (let i = 0; i < localStorage.length; i += 1) {
      const k = localStorage.key(i);
      if (k && k.startsWith(DRAFT_PREFIX)) {
        const val = localStorage.getItem(k);
        if (val && val.trim()) out.add(k);
      }
    }
  } catch {
    /* ignore */
  }
  return out;
}
