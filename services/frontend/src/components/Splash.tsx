/** Full-page loading screen shown while the app finds out who is signed in, so no page flashes before a redirect. */
export function Splash() {
  return (
    <div className="splash" role="status" aria-live="polite" aria-label="Loading">
      <div className="brand">
        <b>Kya Pehnu</b>
        <span lang="hi">क्या पहनूँ</span>
      </div>
      <span className="splash-bar" aria-hidden="true" />
    </div>
  );
}
