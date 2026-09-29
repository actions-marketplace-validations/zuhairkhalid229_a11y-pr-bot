/** @type {import('next').NextConfig} */
export default {
  reactStrictMode: true,
  // Every page is client-rendered against Firestore with the viewer's own
  // credentials, so there is no server-side data fetching and nothing to cache.
};
