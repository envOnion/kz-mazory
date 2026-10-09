ARG NODE_BASE_IMAGE=node:22-alpine
FROM ${NODE_BASE_IMAGE} AS dependencies
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
COPY frontend/scripts ./scripts
RUN npm ci
COPY frontend/index.html frontend/postcss.config.js frontend/tailwind.config.js ./
COPY frontend/tsconfig.json frontend/vite.config.ts ./
COPY frontend/public ./public
COPY frontend/src ./src
RUN npm run build

FROM mcr.microsoft.com/playwright:v1.63.0-noble
WORKDIR /app
COPY --from=dependencies /app/node_modules ./node_modules
COPY --from=dependencies /app/dist ./dist
COPY frontend/package.json frontend/package-lock.json frontend/playwright.local.config.ts ./
COPY frontend/scripts ./scripts
COPY frontend/e2e ./e2e
CMD ["node", "node_modules/@playwright/test/cli.js", "test", "--config=playwright.local.config.ts"]
