import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Navigate, useSearchParams } from "react-router-dom";
import { api, ApiError, unwrap } from "@/api/client";
import { Splash } from "@/components/Splash";

/** What the sign-in page says for each `?error=` reason the backend redirects with. */
const REASONS: Record<string, string> = {
  state: "That sign-in took too long or came from another browser. Please try again.",
  google_error: "Google did not complete the sign-in. Please try again.",
  google_unreachable: "Google could not be reached. Check your connection and try again.",
  token: "Google's response could not be verified. Please try again.",
  unverified_email: "Your Google email address is not verified, so it cannot be used to sign in.",
  auth_not_configured: "Sign-in is not set up on this Kya Pehnu yet. Ask the person running it.",
};
const GENERIC_REASON = "Sign-in failed. Please try again.";
const NOT_CONFIGURED = "auth_not_configured";

const MAX_NEXT_LENGTH = 2048;
const FIRST_PRINTABLE = 0x20;
const DELETE_CHAR = 0x7f;

/** Only a path on this site is a place to go back to. Mirrors the backend's `safe_next`, which checks it again. */
const localPath = (next: string | null): string => {
  if (!next || next.length > MAX_NEXT_LENGTH || !next.startsWith("/") || next.startsWith("//")) return "/";
  const unsafe = (c: string) => c === "\\" || c.charCodeAt(0) < FIRST_PRINTABLE || c.charCodeAt(0) === DELETE_CHAR;
  if ([...next].some(unsafe)) return "/";
  return new URL(next, window.location.origin).origin === window.location.origin ? next : "/";
};

/** Google's "G" mark, as their sign-in branding guidelines draw it. */
function GoogleMark() {
  return (
    <svg viewBox="0 0 48 48" width="20" height="20" aria-hidden="true">
      <path fill="#EA4335" d="M24 9.5c3.5 0 6.6 1.2 9.1 3.6l6.8-6.8C35.8 2.4 30.3 0 24 0 14.6 0 6.6 5.4 2.7 13.3l7.9 6.1C12.5 13.6 17.8 9.5 24 9.5z" />
      <path fill="#4285F4" d="M46.5 24.5c0-1.6-.1-3.1-.4-4.5H24v9h12.7c-.6 3-2.3 5.5-4.8 7.2l7.7 6c4.5-4.2 7-10.3 7-17.7z" />
      <path fill="#FBBC05" d="M10.6 28.6A14.5 14.5 0 0 1 9.5 24c0-1.6.3-3.1.8-4.6l-7.9-6.1A24 24 0 0 0 0 24c0 3.9.9 7.5 2.6 10.7l8-6.1z" />
      <path fill="#34A853" d="M24 48c6.5 0 11.9-2.1 15.9-5.8l-7.7-6c-2.1 1.4-4.9 2.3-8.2 2.3-6.2 0-11.5-4.1-13.4-9.9l-8 6.1C6.6 42.6 14.6 48 24 48z" />
    </svg>
  );
}

/** Sign-in for a computer of one's own: an email, no Google. Offered only when the server was started with it. */
function LocalSignIn({ next }: { next: string }) {
  const qc = useQueryClient();
  const [email, setEmail] = useState("");
  const [failed, setFailed] = useState(false);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    const res = await fetch("/api/auth/dev-login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email }),
    });
    if (!res.ok) return setFailed(true);
    await qc.invalidateQueries({ queryKey: ["me"] });
    window.location.assign(next);
  };
  return (
    <form onSubmit={submit} className="signin-local">
      <input type="email" required placeholder="you@example.com" value={email} onChange={(e) => setEmail(e.target.value)} aria-label="Your email" />
      <button type="submit" className="signin-google">
        <span>Open my wardrobe</span>
      </button>
      {failed && <p className="signin-alert" role="alert">Sign-in failed. Check the email and try again.</p>}
    </form>
  );
}

export default function SignIn() {
  const [params] = useSearchParams();
  const options = useQuery({ queryKey: ["auth-options"], queryFn: () => unwrap(api.GET("/api/auth/options")), retry: false });
  const next = localPath(params.get("next"));
  const reason = params.get("error");
  // Already signed in: nothing to do here. Not configured: say so instead of offering a button that cannot work.
  const me = useQuery({ queryKey: ["me"], queryFn: () => unwrap(api.GET("/api/auth/me")), retry: false });
  if (me.isPending) return <Splash />; // signed in already? then straight on, without flashing the card
  if (me.data) return <Navigate to={next} replace />;
  const notConfigured = (me.error instanceof ApiError && me.error.code === NOT_CONFIGURED) || reason === NOT_CONFIGURED;
  const message = notConfigured && !options.data?.local ? REASONS[NOT_CONFIGURED] : reason ? (REASONS[reason] ?? GENERIC_REASON) : null;
  return (
    <main className="signin">
      <div className="signin-scene" aria-hidden="true">
        <div className="cab">
          <div className="inside" />
          <div className="door l">
            <div className="f">
              <div className="mirror" />
              <span className="handle" />
            </div>
            <div className="b" />
          </div>
          <div className="door r">
            <div className="f">
              <span className="plate" lang="hi">
                क्या पहनूँ
              </span>
              <span className="handle" />
            </div>
            <div className="b" />
          </div>
        </div>
      </div>
      <section className="signin-card" aria-labelledby="signin-title">
        <div className="signin-brand">
          <span className="signin-mark" aria-hidden="true">
            K
          </span>
          <div className="brand">
            <b>Kya Pehnu</b>
            <span lang="hi">क्या पहनूँ</span>
          </div>
        </div>
        <h1 id="signin-title">Welcome</h1>
        <p className="signin-lead">Sign in to open your wardrobe.</p>
        {message && (
          <p className="signin-alert" role="alert">
            {message}
          </p>
        )}
        {options.data?.local && <LocalSignIn next={next} />}
        {!notConfigured && options.data?.google !== false && (
          <a className="signin-google" href={`/api/auth/google/login?next=${encodeURIComponent(next)}`}>
            <GoogleMark />
            <span>Continue with Google</span>
          </a>
        )}
        {options.data?.google !== false && <p className="signin-note">Google shares only your name and email address with Kya Pehnu. No password is stored here.</p>}
      </section>
      <p className="signin-foot">Your clothes, your outfits, your stylist.</p>
    </main>
  );
}
