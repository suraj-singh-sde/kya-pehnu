# ADR-0016: OAuth state, nonce and PKCE in a signed short-lived cookie

Status: Accepted (2026-10-13)

## Context
The Google sign-in needs state and nonce bound to the browser, ten minutes of validity and single use (FR-7, AC-6). The Valkey key kinds are fixed at five (AC-44), so a sixth for sign-in state is not wanted.

## Decision
authlib's Starlette client (`code_challenge_method="S256"`) stores state, nonce, the PKCE verifier and `next` in Starlette's `SessionMiddleware`: cookie `kyapehnu_oauth`, `max_age=600`, SameSite=Lax, path `/api/auth/google`, signed with a key that is random per process. authlib pops the state on use and the callback clears the cookie on every way out.

## Consequences
No custom state code. Another browser has no cookie, so its state check fails. A replayed callback with a copied cookie fails at Google (the code is single-use and bound to the verifier). A backend restart during a sign-in makes the person try again. Rejected: state in Valkey (a sixth key kind).
