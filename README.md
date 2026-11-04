# INF326 — Tarea 2: URL shortener

Implementación de dos arquitecturas de acortamiento de URLs en Python 3.12 con LiteStar, SQLite y `uv`.

| Arquitectura | Redirección | Comportamiento |
| --- | --- | --- |
| v1 | HTTP 302 | Impide almacenar la redirección y registra cada visita mediante gRPC con TLS. |
| v2 | HTTP 301 | Usa caché de respuestas con TTL y limita las peticiones por IP mediante LiteStar. |

El [enunciado original](docs/2026_INF_326_Arquitectura_de_Software_Tarea_2.pdf), la [arquitectura](docs/architecture.md) y la [discusión de trade-offs](docs/tradeoffs.md) están en `docs/`.

## Integrantes

Completar antes de la entrega académica:

| Nombre | Rol |
| --- | --- |
| Pendiente de informar | Pendiente de informar |

## Prerrequisitos

- Docker con soporte BuildKit y Docker Compose v2 o posterior.
- Puertos locales 8443 y 8444 disponibles para los servicios.
- `curl` para los ejemplos de API. No se necesita instalar Python en el host.

## Ejecución

Desde la raíz del repositorio:

```sh
docker compose up -d --build --wait
docker compose ps
```

Se generan automáticamente una CA local y certificados de servidor. Los servicios quedan disponibles en:

- Arquitectura 1: `https://localhost:8443`.
- Arquitectura 2: `https://localhost:8444`.
- Salud: `/health` en ambas direcciones.

El servicio de registro gRPC es interno a la red de Compose. Cada acortador y el registro tienen volúmenes de datos independientes.

Para verificar los certificados desde el host, copiar la CA pública:

```sh
docker compose cp certificates:/certs/ca.crt ./ca.crt
curl --cacert ca.crt https://localhost:8443/health
curl --cacert ca.crt https://localhost:8444/health
```

El certificado es local y no será reconocido automáticamente por el navegador. `ca.crt` está excluido de Git.

Para detener los servicios conservando los datos:

```sh
docker compose down
```

Los certificados se reutilizan mientras sean válidos. Al ejecutar nuevamente `docker compose up`, el servicio de certificados los renueva si les queda como máximo un día de vigencia. Si ya había servicios en ejecución al renovarlos, recrearlos para que carguen los certificados nuevos:

```sh
docker compose up -d --force-recreate --wait
docker compose cp certificates:/certs/ca.crt ./ca.crt
```

## API

Crear una URL en v1:

```sh
curl --cacert ca.crt https://localhost:8443/urls \
  -H 'Content-Type: application/json' \
  -d '{"long_url":"https://usm.cl/informatica/"}'
```

La respuesta HTTP 201 contiene `id`, `hash`, `long_url` y `short_url`. El primer registro de una base vacía tiene el identificador `597652313` y el hash `eRgfR`.

Para consultar la redirección, reemplazar `HASH` por el valor recibido:

```sh
curl --cacert ca.crt -i https://localhost:8443/HASH
```

La respuesta incluye `302`, `Location` y `Cache-Control: no-store`. Para probar v2, repetir la creación y consulta usando el puerto 8444: la redirección será `301` con `Cache-Control: public, max-age=60` de forma predeterminada. Las bases de ambas versiones son independientes.

| Situación | Respuesta |
| --- | --- |
| URL HTTP/HTTPS válida | 201 y nueva URL corta |
| Cuerpo inválido, URL sin esquema o protocolo distinto de HTTP/HTTPS | 400 |
| Hash inexistente | 404 |
| Límite de v2 agotado | 429 |
| Registro gRPC no disponible al resolver en v1 | 503, sin redirección |

Cada `POST` crea una equivalencia nueva, incluso si la URL ya existía. El servicio acepta URLs con esquema explícito, por ejemplo `https://usm.cl/`.

## Configuración

Los valores predeterminados permiten ejecutar el proyecto sin crear archivos adicionales. Para cambiarlos, copiar `.env.example` a `.env` y editarlo:

```sh
cp .env.example .env
```

| Variable de Compose | Predeterminado | Uso |
| --- | --- | --- |
| `V1_PORT` | `8443` | Puerto HTTPS publicado por v1. |
| `V2_PORT` | `8444` | Puerto HTTPS publicado por v2. |
| `V1_PUBLIC_BASE_URL` | `https://localhost:8443` | Origen utilizado en las URLs cortas de v1. |
| `V2_PUBLIC_BASE_URL` | `https://localhost:8444` | Origen utilizado en las URLs cortas de v2. |
| `GRPC_TIMEOUT` | `2` | Tiempo máximo por llamada de registro, en segundos. |
| `CACHE_TTL` | `60` | Vigencia del caché de servidor y navegador en v2, en segundos. |
| `RATE_LIMIT_REQUESTS` | `60` | Máximo de peticiones por IP y minuto en v2. |

Si se cambia un puerto, también se debe ajustar su URL pública. Los certificados generados cubren `localhost`, `127.0.0.1` y los nombres internos de los servicios; usar otro dominio requiere certificados adecuados para ese nombre.

## Pruebas

Construir la imagen de pruebas y ejecutar las pruebas unitarias y smoke sin levantar los servicios:

```sh
docker compose --profile test build tests
docker compose --profile test run --rm --no-deps tests
```

Ejecutar solamente un nivel:

```sh
docker compose --profile test run --rm --no-deps tests uv run --no-sync pytest tests/unit
docker compose --profile test run --rm --no-deps tests uv run --no-sync pytest -m smoke
```

Las pruebas smoke levantan LiteStar y un servidor gRPC con TLS dentro del contenedor, usando archivos temporales e inyección de dependencias.

Las E2E se ejecutan contra contenedores reales y verifican el destino de las redirecciones con un servidor HTTP local de pruebas, sin depender de páginas externas. Utilizar un proyecto Compose separado para aislar sus datos y límites de peticiones:

```sh
V1_PORT=18443 V2_PORT=18444 \
V1_PUBLIC_BASE_URL=https://localhost:18443 V2_PUBLIC_BASE_URL=https://localhost:18444 \
  docker compose -p inf326-e2e up -d --build --wait
docker compose -p inf326-e2e --profile test run --build --rm --no-deps tests \
  uv run --no-sync pytest -m e2e
docker compose -p inf326-e2e down
```

La suite E2E agota deliberadamente la cuota de v2. Para repetirla con los servicios aún levantados, recrear v2 antes de ejecutar las pruebas:

```sh
V2_PORT=18444 V2_PUBLIC_BASE_URL=https://localhost:18444 \
  docker compose -p inf326-e2e up -d --force-recreate --wait shortener-v2
```

Las E2E consultan el registro persistido mediante una conexión SQLite de solo lectura. El volumen del registro permite crear los archivos auxiliares que SQLite necesita para leer una base en modo WAL.

## Calidad y desarrollo

Las dependencias se administran con `uv` y están fijadas en `uv.lock`. El Dockerfile utiliza caché de dependencias, genera los módulos protobuf durante la construcción y separa las herramientas de prueba de la imagen de ejecución. Todos los servicios y las pruebas se ejecutan como un usuario sin privilegios.

Para instalar y ejecutar los hooks de desarrollo en el host, con `uv` disponible:

```sh
uvx --from pre-commit==4.7.0 pre-commit install
uvx --from pre-commit==4.7.0 pre-commit run --all-files
```

Los hooks revisan formato de archivos, sintaxis YAML/JSON/TOML, conflictos de merge y código Python mediante Ruff. `AGENTS.md` y `docs/tasks/` son documentación local y permanecen ignorados.

## Consideraciones de implementación

- SQLite usa transacciones, WAL, consultas parametrizadas y una restricción única sobre el hash. Se verifica la asignación concurrente de identificadores en las pruebas.
- El caché y el rate limiter de v2 viven en memoria y corresponden a un solo proceso. Reiniciar el proceso los vacía; los datos de las URLs permanecen en su volumen.
- El rate limiter utiliza la IP de conexión y no confía en `X-Forwarded-For`. `/health` queda excluido. Las solicitudes servidas desde caché también consumen cuota.
- V1 espera la confirmación del registro antes de redirigir. Una falla de analítica devuelve 503; una falla de red después de persistir el evento puede dejar un registro aunque el cliente haya recibido un error.
- `/health` comprueba la base de URLs de cada API; el contenedor de registro tiene una comprobación TLS independiente.
- Las visitas se guardan con la URL original y un timestamp UTC. No se implementan autenticación, edición ni eliminación de URLs, porque no son parte del alcance funcional del enunciado.
