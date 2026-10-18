ARG NODE_BASE_IMAGE=node:22-alpine
ARG NGINX_BASE_IMAGE=nginx:1.27-alpine
FROM ${NODE_BASE_IMAGE} AS frontend-build

WORKDIR /app

COPY frontend/package.json frontend/package-lock.json ./
COPY frontend/scripts ./scripts
RUN npm ci

COPY frontend/index.html frontend/postcss.config.js frontend/tailwind.config.js ./
COPY frontend/tsconfig.json frontend/vite.config.ts ./
COPY frontend/public ./public
COPY frontend/src ./src

RUN npm run build

FROM ${NGINX_BASE_IMAGE}

RUN rm -f /etc/nginx/conf.d/default.conf

COPY nginx/default.conf /etc/nginx/conf.d/default.conf
ARG AUTH_REQUEST_RATE=2r/s
ARG API_REQUEST_RATE=20r/s
RUN sed -i "s@zone=auth:10m rate=2r/s@zone=auth:10m rate=${AUTH_REQUEST_RATE}@;s@zone=api:10m rate=20r/s@zone=api:10m rate=${API_REQUEST_RATE}@" /etc/nginx/conf.d/default.conf
COPY --from=frontend-build /app/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
