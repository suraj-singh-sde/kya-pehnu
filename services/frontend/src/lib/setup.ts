const KEY = "setupSkipped";

/** "Skip for now" lasts for this browser session; storage can be blocked, then Setup simply shows again. */
export function setupSkipped(): boolean {
  try {
    return sessionStorage.getItem(KEY) === "1";
  } catch {
    return false;
  }
}

export function skipSetup(): void {
  try {
    sessionStorage.setItem(KEY, "1");
  } catch {
    /* ignore */
  }
}
