import { api, ApiError, unwrap } from "@/api/client";

/**
 * Thrown for a place the geocoder does not know by that name, with names close to the spelling (may be empty when
 * offline). `keepable` means the geocoder did match something for the spelling, so the person may keep it anyway.
 */
export class UnknownPlaceError extends Error {
  suggestions: string[];
  keepable: boolean;
  constructor(place: string, suggestions: string[], keepable: boolean) {
    super(
      keepable
        ? `I know a place spelt “${place}”, but it may not be the one you mean.${suggestions.length ? " Did you mean:" : ""}`
        : `I can’t find “${place}”.${suggestions.length ? " Did you mean:" : " Check the spelling, or try the nearest big city."}`,
    );
    this.suggestions = suggestions;
    this.keepable = keepable;
  }
}

/** "Gurugram, Haryana, India" -> "Gurugram": the part the geocoder matches on. */
export const cityOf = (suggestion: string): string => suggestion.split(",")[0].trim();

const same = (a: string, b: string) => a.trim().toLowerCase() === b.trim().toLowerCase();

/**
 * Resolves when the geocoder knows a place by exactly this name. Throws UnknownPlaceError when it knows none, or
 * only something spelt differently (the geocoder matches prefixes, so "Banglore" finds a village called Banglor).
 * Other failures (offline, server) pass: the name still gets refused later if it is wrong.
 */
export async function checkPlace(place: string): Promise<void> {
  let known = true;
  try {
    await unwrap(api.GET("/api/weather", { params: { query: { place } } }));
  } catch (e) {
    if (!(e instanceof ApiError && e.code === "place_not_found")) return;
    known = false;
  }
  const near = await unwrap(api.GET("/api/weather/places", { params: { query: { q: place } } })).catch(() => ({ places: [] as string[] }));
  if (known && (near.places.length === 0 || near.places.some((n) => same(cityOf(n), place)))) return;
  throw new UnknownPlaceError(place, near.places, known);
}
