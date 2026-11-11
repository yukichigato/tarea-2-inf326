# Validación de la entrega

## Entorno y alcance

Auditoría ejecutada el 10 de noviembre de 2026 en Linux/amd64 sobre WSL2, Docker 29.6.2 y Compose 5.3.1. La imagen ejecutó Python 3.12.16 y SQLite 3.40.1; el módulo `ssl` de Python informó OpenSSL 3.0.22. Las versiones Python proceden de `uv.lock`: LiteStar 2.25.0, grpcio 1.85.1, protobuf 7.36.2, Pydantic 2.14.2 y cryptography 50.0.2.

Las imágenes base de Python y `uv` están fijadas por digest en `Dockerfile`. Se construyeron las etapas de pruebas y ejecución con `--no-cache`, utilizando solo archivos versionados como contexto. Se conservaron las imágenes base y la caché de descargas de `uv`; no se reutilizaron capas de instalación ni entornos virtuales del host, ni se instalaron dependencias manualmente. Esta comprobación no certifica otras arquitecturas o sistemas anfitriones.

## Resultados comprobados

| Comprobación | Resultado y evidencia |
| --- | --- |
| Instalación completa | Construcción de las etapas `test` y `runtime` finalizada correctamente. Los tres servicios arrancaron saludables sobre volúmenes nuevos en el proyecto `inf326-audit-20261110`. |
| Dependencias en ejecución | Se importaron las bibliotecas de ejecución; `pytest`, `grpc_tools`, `ruff`, `uv` y `uvx` no están en la imagen final, de 328 MB. El usuario es `app`, UID 10001. |
| Pruebas unitarias y smoke | 55 pruebas pasaron dentro de la imagen construida sin reutilizar capas. |
| E2E | Dos pruebas pasaron contra los contenedores de auditoría: TLS verificado, redirecciones, llegada al destino, eventos persistidos y cuota agotada. |
| Contrato de URL | La prueba parametrizada conserva la URL normalizada y su query en respuesta, `Location` y evento; la prueba gRPC conserva un timestamp UTC inyectado. |
| Concurrencia | 40 inserciones de URLs con ocho trabajadores sin IDs repetidos; 16 llamadas gRPC desde ocho hilos emisores persistidas; ocho GET concurrentes con cuota de tres devolvieron tres 301 y cinco 429. |
| Salud de v1 | Con el contenedor `log` detenido, el healthcheck de v1 terminó con error (`/health` responde 503); al iniciarlo de nuevo, volvió a terminar correctamente. Las pruebas smoke cubren 503 en v1 y 200 en v2 ante un registro inaccesible. |
| IP vista por el rate limiter | Peticiones desde el host a `localhost:18444` y `127.0.0.1:18444` aparecieron en el log de acceso de v2 con `172.20.0.1`, el gateway de la red de Compose; una petición desde otro contenedor apareció con `172.20.0.5`. Los clientes del host comparten cuota. |
| Timeout ambiguo | Una prueba retiene la escritura remota hasta que el cliente recibe `DEADLINE_EXCEEDED`, la libera y verifica que el evento se confirma después del error. |

Los totales de los experimentos son entradas controladas de pruebas; no representan tráfico esperado de la USM. Las E2E usan una página servida por el contenedor de pruebas, no disponibilidad de páginas externas. No se midieron capacidad, percentiles de latencia, distribución de llegadas ni proporciones de caché; no se atribuyen resultados de rendimiento a estas pruebas funcionales.

## Reproducir la auditoría

Desde una revisión guardada en Git, construir únicamente con archivos versionados:

```sh
git archive --format=tar HEAD | docker build --no-cache --target test -t inf326-shortener:test -
git archive --format=tar HEAD | docker build --no-cache --target runtime -t inf326-shortener:runtime -
docker run --rm inf326-shortener:test uv run --no-sync pytest
```

Para la prueba de instalación se usó un proyecto distinto del despliegue de desarrollo y volúmenes nuevos. Elegir un nombre de proyecto que no se haya usado si se quiere repetir esa condición:

```sh
V1_PORT=19443 V2_PORT=19444 \
V1_PUBLIC_BASE_URL=https://localhost:19443 V2_PUBLIC_BASE_URL=https://localhost:19444 \
  docker compose -p inf326-audit-nuevo up -d --no-build --wait
docker compose -p inf326-audit-nuevo --profile test run --rm --no-deps tests \
  uv run --no-sync pytest -m e2e
docker compose -p inf326-audit-nuevo down -v
```

La suite E2E consume la cuota de v2; para repetirla sin detener el proyecto se debe recrear ese servicio, como indica el README. Sin `-v`, `down` conserva los volúmenes y una nueva ejecución con el mismo nombre de proyecto ya no parte de almacenamiento vacío.

## Interpretación de las garantías

Una respuesta 302 confirma que la RPC de registro respondió correctamente antes de construir la redirección. Un 503 no demuestra que no exista un evento. `GRPC_TIMEOUT=2`, `CACHE_TTL=60` y `RATE_LIMIT_REQUESTS=60` son parámetros configurados, no mediciones de latencia, frescura global o capacidad. Las consecuencias de fallas y cambios están descritas en [los trade-offs](tradeoffs.md).
