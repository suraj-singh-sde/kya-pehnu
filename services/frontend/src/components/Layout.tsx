import { useQuery } from "@tanstack/react-query";
import { Link, Navigate, NavLink, Outlet, useLocation } from "react-router-dom";
import { api, unwrap } from "@/api/client";
import { Splash } from "@/components/Splash";

const NAV = [
  { to: "/", label: "Wardrobe", icon: <><rect x="5" y="3" width="14" height="17" rx="1.5" /><path d="M12 3v17M9.5 11v2M14.5 11v2M7 20v1.5M17 20v1.5" /></> },
  { to: "/chat", label: "Stylist", icon: <path d="M12 8a2 2 0 1 0-2-2M12 8v2l8 6a1 1 0 0 1-.6 1.8H4.6A1 1 0 0 1 4 16l8-6" /> },
  { to: "/settings", label: "Account", icon: <><circle cx="12" cy="8" r="4" /><path d="M4 21a8 8 0 0 1 16 0" /></> },
];

export function Layout() {
  const me = useQuery({ queryKey: ["me"], queryFn: () => unwrap(api.GET("/api/auth/me")) });
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => unwrap(api.GET("/api/settings")) });
  const profile = useQuery({ queryKey: ["profile"], queryFn: () => unwrap(api.GET("/api/profile")) });
  const location = useLocation();
  const llm = settings.data?.llm;
  const onSetup = location.pathname === "/setup";
  // Nothing is drawn until the session and the profile are known: a 401 sends the browser to sign-in (api/client)
  // and an unfinished onboarding goes to Welcome, and neither should show the almirah first.
  if (!me.data || !profile.data) return <Splash />;
  // Onboarding asks the gender once; until then there is no almirah to show.
  if (profile.data.gender === "") return <Navigate to="/welcome" replace />;
  return (
    <div className="app">
      <nav aria-label="Main">
        <div className="brand">
          <b>Kya Pehnu</b>
          <span lang="hi">क्या पहनूँ</span>
        </div>
        <div className="tabs">
          {NAV.map((n) => (
            <NavLink key={n.to} to={n.to} end={n.to === "/"}>
              <svg viewBox="0 0 24 24" aria-hidden="true">
                {n.icon}
              </svg>
              {n.label}
            </NavLink>
          ))}
        </div>
        <span className="note" aria-hidden="true" />
      </nav>
      <main>
        <section>
          {llm && !llm.configured && !onSetup && (
            <div className="notice" style={{ marginBottom: 18 }}>
              {me.data?.is_admin === false ? (
                "No AI provider is set up yet, so photo tagging and the stylist will fail. Ask the admin to set up an AI provider."
              ) : (
                <>No AI provider is set up yet, so photo tagging and the stylist will fail. <Link className="lnk" to="/settings">Choose one in Account</Link>.</>
              )}
            </div>
          )}
          <Outlet />
        </section>
      </main>
    </div>
  );
}
