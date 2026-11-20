import { useSyncExternalStore } from "react";

/** The photo in the frame next to the almirah. It stays in this browser: a data URL in localStorage, or the default. */
const KEY = "kyapehnu-frame";
export const FRAME_DEFAULT = "/frame-default.jpg";
const MAX_SIDE = 640;
const listeners = new Set<() => void>();

function read(): string {
  try {
    return localStorage.getItem(KEY) || FRAME_DEFAULT;
  } catch {
    return FRAME_DEFAULT;
  }
}

/** Saves the photo (or clears it with null). Returns false when storage is blocked; the page still shows it this once. */
export function setFrame(data: string | null): boolean {
  let ok = true;
  try {
    if (data) localStorage.setItem(KEY, data);
    else localStorage.removeItem(KEY);
  } catch {
    ok = false;
  }
  listeners.forEach((l) => l());
  return ok;
}

export function useFrame(): string {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
    read,
    () => FRAME_DEFAULT,
  );
}

/** Shrinks an image file to a JPEG data URL small enough for localStorage. Rejects when the file is not an image. */
export function compressImage(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      const k = Math.min(1, MAX_SIDE / Math.max(img.width, img.height));
      const c = document.createElement("canvas");
      c.width = Math.round(img.width * k);
      c.height = Math.round(img.height * k);
      c.getContext("2d")!.drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(url);
      resolve(c.toDataURL("image/jpeg", 0.85));
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("That file is not an image"));
    };
    img.src = url;
  });
}
