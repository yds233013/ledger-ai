# Ledger AI web — production image.
#
# Uses Next.js standalone output, so the runtime layer carries only the server
# and the traced dependencies rather than the whole 479 MB node_modules tree.

# ---------- dependencies ----------
FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json ./
# npm ci installs exactly what the lock file says, and fails if the two disagree.
RUN npm ci

# ---------- build ----------
FROM node:22-alpine AS builder
WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY . .

# NEXT_PUBLIC_* values are inlined at build time, so the API origin has to be
# known here rather than at run time.
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
# Clerk's publishable key is inlined the same way, and its absence is what the
# frontend reads as "Clerk is not configured". Left empty by default, so a build
# without it produces the demo-only application rather than a broken beta page.
# It is a publishable key: it ships in the browser bundle by design and is not
# a secret. The Clerk secret key is never needed at build time.
ARG NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL} \
    NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=${NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY} \
    NEXT_TELEMETRY_DISABLED=1

RUN npm run build

# ---------- runtime ----------
FROM node:22-alpine AS runtime

RUN apk add --no-cache curl

# node:alpine ships a `node` user at uid 1000; reuse it rather than inventing one.
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0

COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
COPY --from=builder --chown=node:node /app/public ./public

USER node
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/sign-in" || exit 1

# The standalone server handles SIGTERM and drains before exiting.
CMD ["node", "server.js"]
