import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api, ApiError, unwrap, type WeatherOut } from "@/api/client";

export const dayPart = (d: Date): string => (d.getHours() < 12 ? "morning" : d.getHours() < 17 ? "afternoon" : "evening");

const WEEKDAY = new Intl.DateTimeFormat(undefined, { weekday: "long" });
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** "Monday morning in Gurugram. Feels like 19 to 20°, no rain expected, AQI 248, poor air." Facts the forecast lacks are left out. */
export function todaySentence(d: Date, place: string, weather: WeatherOut | undefined): string {
  const head = `${WEEKDAY.format(d)} ${dayPart(d)}${place ? ` in ${place}` : ""}.`;
  if (!weather) return head;
  const s = weather.summary;
  const lo = Math.round(s.feels_like_min_c);
  const hi = Math.round(s.feels_like_max_c);
  const parts = [`feels like ${lo === hi ? lo : `${lo} to ${hi}`}°`];
  const rain = s.rain_probability_max_pct;
  if (rain != null) parts.push(rain < 10 ? "no rain expected" : rain < 40 ? "a chance of rain" : "rain likely");
  if (s.wind_max_kmh != null && s.wind_max_kmh >= 25) parts.push("windy");
  if (s.uv_max != null && s.uv_max >= 6) parts.push("strong sun");
  const aqi = s.aqi_max;
  if (aqi != null && aqi > 100) parts.push(`AQI ${Math.round(aqi)}, ${aqi > 300 ? "very poor" : aqi > 200 ? "poor" : "moderate"} air`);
  const tail = s.source === "typical" ? " Typical for this week, not a forecast." : "";
  return `${head} ${cap(parts.join(", "))}.${tail}`;
}

/** The day and the weather in the home city, as one sentence under the page title. */
export function WeatherLine() {
  const profile = useQuery({ queryKey: ["profile"], queryFn: () => unwrap(api.GET("/api/profile")) });
  const place = profile.data?.home_city ?? "";
  const now = useQuery({
    queryKey: ["weather", place],
    queryFn: () => unwrap(api.GET("/api/weather", { params: { query: { place } } })),
    enabled: !!place.trim(),
    staleTime: 10 * 60_000,
    retry: false,
  });
  const unknown = now.error instanceof ApiError && now.error.code === "place_not_found";
  return (
    <p className="today">
      {todaySentence(new Date(), unknown ? "" : place, now.data?.weather ?? undefined)}
      {!place && profile.data && (
        <>
          {" "}
          <Link className="lnk" to="/settings">Set your home city</Link> for the weather.
        </>
      )}
      {unknown && (
        <>
          {" "}
          “{place}” is not a place I know. <Link className="lnk" to="/settings">Fix it in Account</Link>.
        </>
      )}
      {now.error && !unknown && " Weather unavailable."}
    </p>
  );
}
