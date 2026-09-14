FROM node:22-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS webapp-build

WORKDIR /webapp
COPY webapp/package.json webapp/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY webapp/ ./
RUN npm run build

FROM python:3.12-slim@sha256:7a8b475003c4fe15a2cd4e55e5cfc2f3560bdc9333d624f24cdd6d4340fd7a17

WORKDIR /app
COPY pyproject.toml README.md LICENSE /app/
COPY rynmesh /app/rynmesh
COPY --from=webapp-build /webapp/dist /app/rynmesh/webui
RUN --mount=type=cache,target=/root/.cache/pip python -m pip install .

ENTRYPOINT []
