# Security triage log

Dependabot alerts this project has dismissed rather than fixed, and why. A
dismissal without a written reason is indistinguishable from neglect, so every
one is recorded here with the evidence behind it.

The standard for dismissing is that the vulnerable code **demonstrably cannot
run** in this product — not that it seems unlikely to. Where an upgrade exists
and passes the test suite, we upgrade instead.

---

## 2026-10-03 — `@grpc/grpc-js` < 1.13.6 (alerts #79 low, #80 high)

**Decision:** dismissed as `not_used`.

**Where it comes from.** A transitive dependency of the `firebase` npm package
in `web/`: `firebase` → `@firebase/firestore` → `@grpc/grpc-js`.

**Why there is no upgrade.** `firebase` 12.19.0 is the latest release, and
`@firebase/firestore` 4.17.2 pins `@grpc/grpc-js: ~1.9.0`. No published
`firebase` resolves to a patched version.

**Why an npm `overrides` was rejected.** Forcing `@grpc/grpc-js@^1.13.6` would
override Google's explicit tilde pin with an untested jump of four minor
versions, to patch code that never executes here. That trades a cosmetic alert
for a real risk of breaking Firestore's Node transport.

**Why the vulnerable code cannot run.** Both advisories concern grpc-js acting
as a **server**:

| Alert | Advisory | Requires |
|---|---|---|
| #80 high | `getAuthContext` can report unauthorised client certificates as authorised | a grpc-js **server** doing TLS client-certificate auth |
| #79 low | error messages thrown by method handlers are sent to the client | a grpc-js **server** with method handlers |

This application never runs a gRPC server. It is at most a Firestore *client*.

**Verified against the production build** (`npm run build`, then searching
`.next/`):

| Marker | Client bundle (ships to browsers) | SSR chunks |
|---|---|---|
| `getAuthContext` — the vulnerable function | absent | **absent** |
| `@grpc/grpc-js` | absent | referenced |
| `Http2Session` / `http2.connect` — its transport | absent | absent |
| `WebChannel` — browser Firestore transport | present | absent |

Browsers get Firestore over WebChannel and never load grpc-js. The SSR build
references the package name but does not contain the vulnerable function, and
in practice SSR never opens a Firestore connection at all: every dashboard page
is `"use client"` and fetches inside `useEffect`, so the server renders only a
loading shell.

**Revisit when** a `firebase` release depends on `@grpc/grpc-js >= 1.13.6`.
Dependabot's monthly npm group will propose it; at that point upgrade and close
this entry. To re-check by hand:

```bash
cd web
npm ls @grpc/grpc-js --all
npm run build
grep -rl getAuthContext .next/   # must stay empty
```

---

## 2026-09-29 — `tools/package-lock.json` (41 alerts)

**Decision:** stopped tracking the file; alerts closed automatically.

All 41 came from the transitive tree of `firebase-tools` and `vercel`, which
are deploy CLIs installed under `tools/`. They are never deployed, never
imported by the product, and never handle user data. `tools/package.json` still
pins both CLI versions.
